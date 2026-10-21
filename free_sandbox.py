"""Max free cloud sandbox + GitHub-hosted memory ("brain") layer.

Sandbox (OnlineCompiler.io free tier, verified against their docs):

    POST https://api.onlinecompiler.io/api/run-code-sync/
    Authorization: <your api key>          # no "Bearer" prefix
    {"compiler": "python-3.14", "code": "...", "input": "..."}

Documented free-plan limits: 100,000 runs/month, 30 submissions/minute,
2 concurrent executions, 30s execution timeout, 100KB code and stdin each,
64KB output per stream, and **no outbound network inside the sandbox**.

The brain lives in ``brain_memory.txt`` inside the repository Max works on,
so every lesson survives restarts and is reviewable in GitHub history.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from github import Github, GithubException, UnknownObjectException

SANDBOX_BASE_URL = os.getenv("ONLINE_COMPILER_BASE_URL", "https://api.onlinecompiler.io").rstrip("/")
SANDBOX_API_KEY = os.getenv("ONLINE_COMPILER_API_KEY")
DEFAULT_COMPILER = os.getenv("SANDBOX_COMPILER", "python-3.14")
HTTP_TIMEOUT_SECONDS = float(os.getenv("SANDBOX_HTTP_TIMEOUT", "60"))

MAX_CODE_BYTES = 100 * 1024  # provider limit for `code`
MAX_STDIN_BYTES = 100 * 1024  # provider limit for `input`

BRAIN_FILE = "brain_memory.txt"
BRAIN_HEADER = "# Max Core Memory Bank"
BRAIN_MAX_CHARS = 12_000  # keep the diary small enough to fit a single prompt
MEMORY_WRITES_ENABLED = False  # remote memory writes need scoped approval

CLIP_DEFAULT = 1500
PROMPT_CLIP = 4_000

_github: Optional[Github] = None


def clip(text: str, limit: int = CLIP_DEFAULT) -> str:
    """Shorten long text so Discord messages stay under their 2000-char cap."""
    text = text or ""
    if len(text) <= limit:
        return text
    marker = "\n… [truncated]"
    return text[: limit - len(marker)] + marker


def _client() -> Github:
    global _github
    if _github is None:
        token = os.getenv("GITHUB_TOKEN")
        _github = Github(token) if token else Github()
    return _github


# --------------------------------------------------------------------------
# Sandbox execution
# --------------------------------------------------------------------------
@dataclass
class SandboxResult:
    ok: bool                     # False => transport/config problem, no code ran
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration: str = ""
    error: str = ""

    @property
    def passed(self) -> bool:
        """True only when the code actually executed and exited cleanly."""
        return self.ok and self.exit_code == 0

    def for_prompt(self, label: str = "sandbox run") -> str:
        """Compact text report for feeding back into the LLM prompt."""
        if not self.ok:
            return f"[{label}] did not execute: {self.error}"
        lines = [f"[{label}] exit code: {self.exit_code}"]
        if self.duration:
            lines.append(f"time: {self.duration}")
        if self.stderr:
            lines.append(f"stderr:\n{self.stderr}")
        if self.stdout:
            lines.append(f"stdout:\n{self.stdout}")
        if self.exit_code == 0 and not self.stderr and not self.stdout:
            lines.append("(no output)")
        return clip("\n".join(lines), PROMPT_CLIP)

    def for_discord(self) -> str:
        """Human-readable report for a Discord message."""
        if not self.ok:
            return f"⚠️ {self.error}"
        if self.passed:
            timing = f" ({self.duration})" if self.duration else ""
            body = clip(self.stdout) or "(no output)"
            return f"✅ Exit 0{timing}\n```\n{body}\n```"
        body = clip(self.stderr or self.stdout or "(no output)")
        return f"❌ Exit {self.exit_code}" + (f" ({self.duration})" if self.duration else "") + f"\n```\n{body}\n```"


def _parse_response(data: dict) -> SandboxResult:
    # Piston-compatible response shape nests everything under `run`.
    if isinstance(data.get("run"), dict):
        run = data["run"]
        return SandboxResult(
            ok=True,
            exit_code=run.get("code"),
            stdout=run.get("stdout") or "",
            stderr=run.get("stderr") or "",
            duration=str(run.get("cpu_time") or run.get("time") or ""),
        )

    # Native sync shape: {status, exit_code, output, error, time, total, memory, ...}
    if data.get("exit_code") is None:
        return SandboxResult(ok=False, error="Unexpected sandbox response shape.")

    return SandboxResult(
        ok=True,
        exit_code=int(data["exit_code"]),
        stdout=data.get("output") or data.get("stdout") or "",
        stderr=data.get("error") or data.get("stderr") or "",
        duration=str(data.get("time") or ""),
    )


def run_code(code: str, stdin: str = "", compiler: str = DEFAULT_COMPILER) -> SandboxResult:
    """Execute ``code`` in the free OnlineCompiler.io container."""
    if not SANDBOX_API_KEY:
        return SandboxResult(
            ok=False,
            error=(
                "ONLINE_COMPILER_API_KEY is not set. Add it to Railway Variables "
                "(or Settings → Environment) so sandbox validation can run."
            ),
        )
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return SandboxResult(ok=False, error="Code exceeds the sandbox's 100KB limit.")
    if len(stdin.encode("utf-8")) > MAX_STDIN_BYTES:
        return SandboxResult(ok=False, error="Stdin exceeds the sandbox's 100KB limit.")

    try:
        response = requests.post(
            f"{SANDBOX_BASE_URL}/api/run-code-sync/",
            json={"compiler": compiler, "code": code, "input": stdin},
            headers={"Authorization": SANDBOX_API_KEY, "Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        return SandboxResult(ok=False, error=f"Sandbox did not respond within {HTTP_TIMEOUT_SECONDS:.0f}s.")
    except requests.RequestException:
        return SandboxResult(ok=False, error="Could not reach the sandbox API.")

    if response.status_code == 401 or response.status_code == 403:
        return SandboxResult(ok=False, error="Sandbox rejected the API key (HTTP "
                             f"{response.status_code}) — check ONLINE_COMPILER_API_KEY.")
    if response.status_code == 429:
        return SandboxResult(
            ok=False,
            error="Sandbox rate limit or monthly free-quota reached (HTTP 429). "
                  "Free plan: 30 submissions/min, 100k runs/month.",
        )
    if response.status_code != 200:
        # The API reports bad/expired keys with 404 + {"error": "Invalid or inactive API key"}.
        if "api key" in response.text.lower():
            return SandboxResult(
                ok=False,
                error=(
                    "Sandbox rejected ONLINE_COMPILER_API_KEY "
                    f"(HTTP {response.status_code}) — generate a REST key on the "
                    "OnlineCompiler.io dashboard's API Keys page."
                ),
            )
        return SandboxResult(ok=False, error=f"Sandbox API returned HTTP {response.status_code}.")

    try:
        data = response.json()
    except ValueError:
        return SandboxResult(ok=False, error="Sandbox API returned a non-JSON response.")

    if not isinstance(data, dict):
        return SandboxResult(ok=False, error="Unexpected sandbox payload shape.")
    return _parse_response(data)


def test_code_in_free_sandbox(
    ai_generated_code: str,
    compiler: str = DEFAULT_COMPILER,
    stdin: str = "",
) -> str:
    """Convenience wrapper: run code, return a short text report (for LLM prompts)."""
    return run_code(ai_generated_code, stdin=stdin, compiler=compiler).for_prompt()


# --------------------------------------------------------------------------
# GitHub-hosted brain
# --------------------------------------------------------------------------
def _trim_brain(content: str) -> str:
    if len(content) <= BRAIN_MAX_CHARS:
        return content
    # Drop the oldest bullet points first, keep the header.
    lines = content.splitlines()
    header = lines[0] if lines and lines[0].startswith("#") else BRAIN_HEADER
    kept: list[str] = []
    size = len(header)
    for line in reversed(lines[1:] if header in lines else lines):
        if size + len(line) + 1 > BRAIN_MAX_CHARS:
            break
        kept.append(line)
        size += len(line) + 1
    kept.reverse()
    return header + "\n" + "\n".join(kept) + "\n"


def read_flanx_brain(repo_name: str, ref: str = "main") -> str:
    """Read Max's historical memory log from GitHub."""
    try:
        repo = _client().get_repo(repo_name)
        file_obj = repo.get_contents(BRAIN_FILE, ref=ref)
        content = file_obj.decoded_content.decode("utf-8", errors="replace")
        if len(content) > BRAIN_MAX_CHARS:
            content = "…[older memories trimmed]…" + content[-BRAIN_MAX_CHARS:]
        return content
    except UnknownObjectException:
        return "No repository memory found yet."
    except Exception:  # never let memory trouble kill a run
        return "Repository memory unavailable right now."


def update_flanx_brain(
    repo_name: str,
    new_insight: str,
    ref: str = "main",
    max_retries: int = 3,
) -> bool:
    """Append a timestamped lesson to ``brain_memory.txt`` on the target branch."""
    if not MEMORY_WRITES_ENABLED:
        return False
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"- [{stamp}] {new_insight}"
    try:
        repo = _client().get_repo(repo_name)
    except Exception:
        print("⚠️ Brain update warning (repo unavailable).")
        return False

    for attempt in range(max_retries):
        try:
            try:
                file_obj = repo.get_contents(BRAIN_FILE, ref=ref)
                current = file_obj.decoded_content.decode("utf-8", errors="replace")
                sha: Optional[str] = file_obj.sha
            except UnknownObjectException:
                current, sha = f"{BRAIN_HEADER}\n", None

            updated = _trim_brain(f"{current.rstrip()}\n{entry}\n")
            if sha:
                repo.update_file(
                    BRAIN_FILE,
                    "🤖 Max memory updated.",
                    updated,
                    sha,
                    branch=ref,
                )
            else:
                repo.create_file(
                    BRAIN_FILE,
                    "🤖 Max brain initialization.",
                    updated,
                    branch=ref,
                )
            print("🧠 Max wrote a lesson to its memory layer.")
            return True
        except GithubException as exc:
            if exc.status == 409 and attempt < max_retries - 1:
                continue  # concurrent write, refetch and retry
            print(f"⚠️ Brain update warning (HTTP {exc.status}).")
            return False
        except Exception:
            print("⚠️ Brain update warning (write failed).")
            return False
    return False
