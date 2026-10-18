"""Flanx free cloud sandbox + GitHub-hosted memory ("brain") layer.

Sandbox (OnlineCompiler.io free tier, verified against their docs):

    POST https://api.onlinecompiler.io/api/run-code-sync/
    Authorization: <your api key>          # no "Bearer" prefix
    {"compiler": "python-3.14", "code": "...", "input": "..."}

The provider documents 30 submissions/minute, 2 concurrent executions,
30s execution timeout, 100KB code and stdin each, 64KB output per stream,
and **no outbound network inside the sandbox**. Check account quota via
/api/usage/ because published monthly-limit pages have differed.

The brain lives in ``brain_memory.txt`` inside the repository Flanx works on,
so every lesson survives restarts and is reviewable in GitHub history.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from github import Github, GithubException, UnknownObjectException

SANDBOX_BASE_URL = os.getenv("ONLINE_COMPILER_BASE_URL", "https://api.onlinecompiler.io").rstrip("/")
SANDBOX_API_KEY = os.getenv("ONLINE_COMPILER_API_KEY")
DEFAULT_COMPILER = os.getenv("SANDBOX_COMPILER", "python-3.14")
try:
    HTTP_TIMEOUT_SECONDS = max(5.0, min(60.0, float(os.getenv("SANDBOX_HTTP_TIMEOUT", "45"))))
except (TypeError, ValueError):
    HTTP_TIMEOUT_SECONDS = 45.0

MAX_CODE_BYTES = 100_000  # documented UTF-8 encoded-byte limit
MAX_STDIN_BYTES = 100_000

BRAIN_FILE = "brain_memory.txt"
BRAIN_HEADER = "# Flanx Core Memory Bank"
BRAIN_MAX_CHARS = 12_000  # keep the diary small enough to fit a single prompt

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
            body = clip(self.stdout).replace("```", "'''") or "(no output)"
            return f"✅ Exit 0{timing}\n```\n{body}\n```"
        body = clip(self.stderr or self.stdout or "(no output)").replace("```", "'''")
        return f"❌ Exit {self.exit_code}" + (f" ({self.duration})" if self.duration else "") + f"\n```\n{body}\n```"


def _parse_response(data: dict) -> SandboxResult:
    """Parse documented native/Piston formats; never echo raw upstream JSON."""
    if not isinstance(data, dict):
        return SandboxResult(ok=False, error="Sandbox returned an invalid response shape.")
    if data.get("truncated") is True:
        return SandboxResult(ok=False, error="Sandbox output was truncated; validation is inconclusive.")
    run = data.get("run") if isinstance(data.get("run"), dict) else data
    native = run is data
    raw_code = run.get("exit_code") if native else run.get("code")
    try:
        if isinstance(raw_code, bool) or raw_code is None:
            raise ValueError("missing exit status")
        code = int(raw_code)
        if not -255 <= code <= 255:
            raise ValueError("invalid exit status")
        if isinstance(raw_code, str) and str(code) != raw_code:
            raise ValueError("invalid exit status")
    except (ValueError, TypeError):
        return SandboxResult(ok=False, error="Sandbox returned no valid exit code.")
    status = data.get("status")
    if status is not None and status not in {"success", "error"}:
        return SandboxResult(ok=False, error="Sandbox returned an unknown execution status.")
    if status == "error" and code == 0:
        return SandboxResult(ok=False, error="Sandbox response had conflicting execution status.")
    stdout = run.get("output", run.get("stdout", "")) if native else run.get("stdout", "")
    stderr = run.get("error", run.get("stderr", "")) if native else run.get("stderr", "")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return SandboxResult(ok=False, error="Sandbox returned malformed output fields.")
    duration = run.get("time", run.get("cpu_time", ""))
    return SandboxResult(
        ok=True,
        exit_code=code,
        stdout=clip(stdout, 64_000),
        stderr=clip(stderr, 64_000),
        duration=str(duration or "")[:32],
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
    if not isinstance(code, str) or not isinstance(stdin, str):
        return SandboxResult(ok=False, error="Sandbox code and input must be text.")
    if "\x00" in code or "\x00" in stdin:
        return SandboxResult(ok=False, error="Sandbox code and input cannot contain NUL bytes.")
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
        return SandboxResult(ok=False, error="Could not connect to the sandbox API. No execution was confirmed.")

    if response.status_code == 401 or response.status_code == 403:
        return SandboxResult(ok=False, error="Sandbox rejected the API key (HTTP "
                             f"{response.status_code}) — check ONLINE_COMPILER_API_KEY.")
    if response.status_code == 429:
        return SandboxResult(
            ok=False,
            error="Sandbox returned HTTP 429 (rate limit, concurrency, or account quota). "
                  "Check the provider's /api/usage/ and /api/limits/ endpoints.",
        )
    if response.status_code != 200:
        if response.status_code in {404, 405}:
            detail = "Check the documented /api/run-code-sync/ endpoint and REST API key."
        elif response.status_code in {400, 413, 415, 422}:
            detail = "Check the compiler ID and submitted code/input limits."
        elif response.status_code >= 500:
            detail = "The provider may be temporarily unavailable."
        else:
            detail = "Check the configured API endpoint and account limits."
        return SandboxResult(
            ok=False,
            error=f"Sandbox API returned HTTP {response.status_code}. {detail}",
        )

    try:
        data = response.json()
    except ValueError:
        return SandboxResult(ok=False, error="Sandbox API returned a non-JSON response.")

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
    """Read Flanx's historical memory log from GitHub."""
    try:
        repo = _client().get_repo(repo_name)
        file_obj = repo.get_contents(BRAIN_FILE, ref=ref)
        content = file_obj.decoded_content.decode("utf-8", errors="replace")
        if len(content) > BRAIN_MAX_CHARS:
            content = "…[older memories trimmed]…" + content[-BRAIN_MAX_CHARS:]
        return content
    except UnknownObjectException:
        return "No memory file found on that branch; historical existence is unknown."
    except Exception:  # never let memory trouble kill a run
        return "Memory unavailable; GitHub access or file retrieval failed."


def update_flanx_brain(
    repo_name: str,
    new_insight: str,
    ref: str = "main",
    max_retries: int = 3,
) -> bool:
    """Append a timestamped lesson to ``brain_memory.txt`` on the target branch."""
    if ref in {"main", "master"}:
        print("⚠️ Refusing to commit brain memory directly to a default branch.")
        return False
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"- [{stamp}] {new_insight}"
    try:
        repo = _client().get_repo(repo_name)
    except Exception:
        print("⚠️ Brain update warning (repository unavailable).")
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
                    "🤖 Flanx brain evolution matrix updated.",
                    updated,
                    sha,
                    branch=ref,
                )
            else:
                repo.create_file(
                    BRAIN_FILE,
                    "🤖 Flanx brain initialization.",
                    updated,
                    branch=ref,
                )
            print("🧠 Flanx wrote a lesson to its memory layer.")
            return True
        except GithubException as exc:
            if exc.status == 409 and attempt < max_retries - 1:
                continue  # concurrent write, refetch and retry
            print(f"⚠️ Brain update failed (GitHub HTTP {exc.status}).")
            return False
        except Exception:
            print("⚠️ Brain update failed.")
            return False
    return False