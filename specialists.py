"""Bounded specialist workflow over the existing task engine.

Adapters supply planner/coder output. No model is called by default, and no
repository or production write is available to any role here.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable

from model_router import ModelRouter
from task_engine import Orchestrator, Status, Task


ROLE_TOOL = {
    "planner": "model.reply",
    "coder": "workspace.edit",
    "reviewer": "workspace.review",
    "tester": "workspace.test",
}
ROLE_ROUTE = {"planner": "general", "coder": "coding", "reviewer": "review", "tester": "review"}
MAX_FILES = 8
MAX_STEPS = 8
MAX_FILE_BYTES = 100_000
MAX_DIFF_BYTES = 60_000
MAX_CORRECTIONS = 2
TEST_COMMANDS = {
    "compile": (sys.executable, "-I", "-S", "-B", "-m", "compileall", "-q", "-f", "."),
    "unittest": (sys.executable, "-I", "-S", "-B", "-m", "unittest", "discover", "-s", "tests", "-q"),
}
_ACTIVE_WORKSPACES: set[Path] = set()
Agent = Callable[[dict], Awaitable[dict]]
_SECRET = re.compile(r"(?i)\b(api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+|"
                     r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}")


def _redact(text: str) -> str:
    return _SECRET.sub("<redacted>", text)


def configured_role_models(environ=None) -> dict[str, str]:
    """Reuse the existing router; these identifiers are advisory to adapters."""
    router = ModelRouter(environ=environ)
    return {role: router.selected_model(role=role) for role in ROLE_ROUTE}


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Invalid workspace path")
    path = PurePosixPath(value)
    if (path.is_absolute() or any(part in (".", "..", ".git", ".env") for part in path.parts)
            or value.startswith("/") or value.endswith("/")):
        raise ValueError("Invalid workspace path")
    return str(path)


def _short(value: object, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and "\x00" not in value


def valid_plan(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"steps"}:
        return False
    steps = value["steps"]
    return (isinstance(steps, list) and 1 <= len(steps) <= MAX_STEPS
            and all(isinstance(step, dict) and set(step) == {"action", "outcome", "verify"}
                    and all(_short(step[key], 240) and not _SECRET.search(step[key])
                            for key in ("action", "outcome", "verify"))
                    for step in steps))


def valid_coder(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"changes", "summary"}:
        return False
    changes = value["changes"]
    if (not isinstance(changes, dict) or not 1 <= len(changes) <= MAX_FILES
            or not _short(value["summary"], 500) or _SECRET.search(value["summary"])):
        return False
    try:
        return all(_path(name) == name and isinstance(content, str)
                   and len(content.encode("utf-8")) <= MAX_FILE_BYTES
                   for name, content in changes.items())
    except (ValueError, UnicodeError):
        return False


def valid_review(value: object) -> bool:
    return (isinstance(value, dict) and set(value) == {"diff_sha256", "findings"}
            and isinstance(value["diff_sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", value["diff_sha256"]) is not None
            and isinstance(value["findings"], list) and len(value["findings"]) <= 20
            and all(isinstance(item, dict) and set(item) == {"severity", "message", "evidence"}
                    and item["severity"] in ("low", "medium", "high")
                    and _short(item["message"], 240) and _short(item["evidence"], 240)
                    and not _SECRET.search(item["message"] + item["evidence"])
                    for item in value["findings"]))


def valid_test(value: object) -> bool:
    return (isinstance(value, dict) and set(value) == {"check", "command", "exit_code", "timed_out", "skipped", "output", "summary"}
            and value["check"] in TEST_COMMANDS
            and value["command"] == " ".join(TEST_COMMANDS[value["check"]])
            and (value["exit_code"] is None or type(value["exit_code"]) is int)
            and type(value["timed_out"]) is bool and type(value["skipped"]) is bool
            and isinstance(value["output"], str) and len(value["output"]) <= 16_000
            and _short(value["summary"], 160))


class SpecialistRegistry:
    """Role registration and execution, with task-engine state and capability checks."""

    def __init__(self, *, store_path: str | None = None):
        self.engine = Orchestrator(store_path=store_path)
        self._registered: set[str] = set()

    def register(self, role: str, handler: Agent) -> None:
        if role not in ROLE_TOOL or role in self._registered:
            raise ValueError("Unknown or duplicate specialist role")
        validator = {"planner": valid_plan, "coder": valid_coder,
                     "reviewer": valid_review, "tester": valid_test}[role]

        async def wrapped(task: Task, payload: dict) -> dict:
            return await handler(payload)

        self.engine.register(f"specialist.{role}", ROLE_TOOL[role], wrapped,
                             valid_input=lambda value: isinstance(value, dict),
                             valid_result=validator, role=role)
        self._registered.add(role)

    async def run(self, role: str, payload: dict, *, timeout: float = 30) -> dict:
        if role not in self._registered:
            raise ValueError("Specialist role is not registered")
        task = Task.new(f"Run {role} specialist", f"specialist.{role}",
                        permitted_tools={ROLE_TOOL[role]}, requested_role=role,
                        timeout_seconds=timeout)
        outcome = await self.engine.run(task, payload)
        if outcome.task.status != Status.COMPLETED:
            raise RuntimeError(f"{role} failed: {outcome.task.error_code or 'unavailable'}")
        return outcome.result


class ScopedWorkspace:
    """Copy explicitly selected text files; never edit the source tree."""

    def __init__(self, source: Path, editable: tuple[str, ...], support: tuple[str, ...] = ()):
        if not source.is_dir() or not 1 <= len(editable) <= MAX_FILES or len(support) > 24:
            raise ValueError("A scoped source workspace is required")
        self.source = source.resolve(strict=True)
        self.editable = tuple(_path(item) for item in editable)
        if len(set(self.editable)) != len(self.editable):
            raise ValueError("Duplicate editable path")
        self.support = tuple(_path(item) for item in support)
        self._temporary = tempfile.TemporaryDirectory(prefix="max-workflow-")
        self.root = Path(self._temporary.name).resolve()
        self.original: dict[str, str] = {}
        try:
            for name in set(self.editable + self.support):
                source_file = self.source / name
                if (not source_file.is_file() or source_file.is_symlink()
                        or not source_file.resolve(strict=True).is_relative_to(self.source)):
                    raise ValueError("Workspace source is missing or unsafe")
                data = source_file.read_bytes()
                if len(data) > MAX_FILE_BYTES:
                    raise ValueError("Workspace source file is too large")
                content = data.decode("utf-8")
                destination = self.root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8", newline="")
                if name in self.editable:
                    self.original[name] = content
            _ACTIVE_WORKSPACES.add(self.root)
        except Exception:
            self.close()
            raise

    def close(self):
        _ACTIVE_WORKSPACES.discard(self.root)
        self._temporary.cleanup()

    def read_editable(self) -> dict[str, str]:
        return {name: (self.root / name).read_text(encoding="utf-8") for name in self.editable}

    def apply(self, changes: dict[str, str]) -> None:
        if not changes or not set(changes).issubset(self.editable):
            raise ValueError("Coder change exceeds workspace scope")
        for name, content in changes.items():
            destination = self.root / name
            if destination.is_symlink() or not destination.resolve().is_relative_to(self.root):
                raise ValueError("Unsafe workspace destination")
            destination.write_text(content, encoding="utf-8", newline="")

    def diff(self) -> tuple[tuple[str, ...], str]:
        changed = []
        parts = []
        for name in self.editable:
            after = (self.root / name).read_text(encoding="utf-8")
            before = self.original[name]
            if after != before:
                changed.append(name)
                parts.extend(difflib.unified_diff(before.splitlines(keepends=True),
                                                   after.splitlines(keepends=True),
                                                   fromfile=f"a/{name}", tofile=f"b/{name}"))
        diff = "".join(parts)
        if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
            raise ValueError("Proposed diff is too large")
        return tuple(changed), diff


async def review_diff(payload: dict) -> dict:
    """Inspect the actual diff; conservative static findings are evidence, not approval."""
    diff = payload["diff"]
    findings = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if re.search(r"(?i)(?:api[_-]?key|password|secret|token)\s*=\s*['\"][^'\"]+", line):
            findings.append({"severity": "high", "message": "Possible hard-coded credential",
                             "evidence": "Added line assigns a credential-like value (redacted)"})
        elif re.search(r"\b(?:subprocess(?:\.[A-Za-z_]+)?|os\.system|eval|exec)\s*\(", line):
            findings.append({"severity": "medium", "message": "Execution call needs review",
                             "evidence": _redact(line[:240])})
    return {"diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            "findings": findings[:20]}


def _check_config() -> tuple[float, int]:
    try:
        timeout = min(30.0, max(0.1, float(os.getenv("MAX_CHECK_TIMEOUT_SECONDS", "10"))))
    except ValueError:
        timeout = 10.0
    try:
        output_limit = min(16_000, max(256, int(os.getenv("MAX_CHECK_OUTPUT_BYTES", "4000"))))
    except ValueError:
        output_limit = 4_000
    return timeout, output_limit


async def _terminate_process(process) -> bool:
    """Terminate the direct child; descendants cannot be guaranteed stopped."""
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError:
            return False
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
        return True
    except asyncio.TimeoutError:
        return False


async def _capture(stream, limit: int) -> bytes:
    chunks = []
    captured = 0
    while data := await stream.read(4096):
        if captured < limit:
            piece = data[:limit - captured]
            chunks.append(piece)
            captured += len(piece)
    return b"".join(chunks)


async def run_check(payload: dict) -> dict:
    if (not isinstance(payload, dict) or not isinstance(payload.get("command"), str)
            or not isinstance(payload.get("workspace"), str)):
        raise ValueError("Invalid check request")
    command = payload["command"]
    root = Path(payload["workspace"])
    if command not in TEST_COMMANDS or not root.is_dir() or root.resolve() not in _ACTIVE_WORKSPACES:
        raise ValueError("Test command or workspace is not permitted")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("Symlinks are not allowed in the check workspace")
    configured_timeout, output_limit = _check_config()
    try:
        timeout = min(configured_timeout, max(0.1, float(payload.get("timeout", configured_timeout))))
    except (TypeError, ValueError):
        raise ValueError("Invalid check timeout") from None
    command_text = " ".join(TEST_COMMANDS[command])
    def report(code=None, *, timed_out=False, skipped=False, output="", summary=""):
        return {"check": command, "command": command_text, "exit_code": code,
                "timed_out": timed_out, "skipped": skipped,
                "output": output[:output_limit], "summary": summary[:160]}

    if command == "unittest":
        return report(skipped=True, summary="skipped: executable tests require stronger isolation")
    if not any(root.rglob("*.py")):
        return report(skipped=True, summary="skipped: no Python files")

    # The copied workspace is a working directory, not a filesystem boundary.
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    environment.update({"TEMP": str(root), "TMP": str(root), "PYTHONIOENCODING": "utf-8"})
    try:
        process = await asyncio.create_subprocess_exec(
            *TEST_COMMANDS[command], cwd=str(root), env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return report(output="Check process could not start.", summary="failed: process unavailable")
    stdout_reader = asyncio.create_task(_capture(process.stdout, output_limit // 2))
    stderr_reader = asyncio.create_task(_capture(process.stderr, output_limit // 2))
    timed_out = False
    terminated = True
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        terminated = await _terminate_process(process)
    except asyncio.CancelledError:
        await _terminate_process(process)
        stdout_reader.cancel()
        stderr_reader.cancel()
        raise
    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.gather(stdout_reader, stderr_reader), timeout=2)
    except asyncio.TimeoutError:
        stdout_reader.cancel()
        stderr_reader.cancel()
        stdout, stderr = b"", b""
        terminated = False
    output = _redact((stdout + b"\n" + stderr).decode("utf-8", errors="replace"))
    if timed_out:
        detail = ("Direct process termination requested; child processes may continue."
                  if terminated else "Process termination could not be confirmed.")
        return report(timed_out=True, output=detail, summary="timed out: termination uncertain")
    return report(process.returncode, output=output,
                  summary="passed" if process.returncode == 0 else "failed")


@dataclass(frozen=True)
class WorkflowReport:
    success: bool
    changed_files: tuple[str, ...]
    plan: dict | None
    coder_summary: str
    review: dict | None
    tests: tuple[dict, ...]
    corrections: int
    diff: str
    error: str | None = None


class CodingWorkflow:
    def __init__(self, source: Path, planner: Agent, coder: Agent,
                 *, task_store_path: str | None = None):
        self.source = Path(source)
        self.registry = SpecialistRegistry(store_path=task_store_path)
        self.registry.register("planner", planner)
        self.registry.register("coder", coder)
        self.registry.register("reviewer", review_diff)
        self.registry.register("tester", run_check)

    async def run(self, goal: str, files: tuple[str, ...], *, support: tuple[str, ...] = (),
                  checks: tuple[str, ...] = ("compile",), max_corrections: int = MAX_CORRECTIONS) -> WorkflowReport:
        try:
            return await asyncio.wait_for(
                self._run_scoped(goal, files, support=support, checks=checks,
                                 max_corrections=max_corrections), timeout=120)
        except asyncio.TimeoutError:
            return WorkflowReport(False, (), None, "", None, (), 0, "", "Workflow timed out")

    async def _run_scoped(self, goal: str, files: tuple[str, ...], *, support: tuple[str, ...] = (),
                  checks: tuple[str, ...] = ("compile",), max_corrections: int = MAX_CORRECTIONS) -> WorkflowReport:
        if not _short(goal, 500) or _SECRET.search(goal) or not 0 <= max_corrections <= MAX_CORRECTIONS:
            raise ValueError("Invalid coding request or correction limit")
        if not checks or any(command not in TEST_COMMANDS for command in checks):
            raise ValueError("Only named test commands are permitted")
        try:
            workspace = ScopedWorkspace(self.source, files, support)
        except (ValueError, OSError, UnicodeError):
            return WorkflowReport(False, (), None, "", None, (), 0, "",
                                  "Isolated workspace could not be established")
        plan = None
        summary = ""
        review = None
        tests = ()
        changed = ()
        diff = ""
        try:
            plan = await self.registry.run("planner", {"goal": goal, "files": list(workspace.editable)})
            for cycle in range(max_corrections + 1):
                result = await self.registry.run("coder", {
                    "goal": goal, "plan": plan, "files": workspace.read_editable(),
                    "feedback": {"review": review, "tests": tests} if cycle else None,
                })
                workspace.apply(result["changes"])
                summary = result["summary"]
                changed, diff = workspace.diff()
                if not changed:
                    return WorkflowReport(False, (), plan, summary, None, (), cycle, "", "No files changed")
                review = await self.registry.run("reviewer", {"diff": diff})
                if review["diff_sha256"] != hashlib.sha256(diff.encode("utf-8")).hexdigest():
                    raise ValueError("Reviewer did not inspect the current diff")
                tests = tuple([await self.registry.run("tester", {
                    "workspace": str(workspace.root), "command": command, "timeout": 10,
                }, timeout=15) for command in checks])
                changed_after_tests, diff_after_tests = workspace.diff()
                if changed_after_tests != changed or diff_after_tests != diff:
                    raise ValueError("Tests modified the proposed workspace diff")
                review_passed = not any(item["severity"] in ("high", "medium") for item in review["findings"])
                tests_passed = all(test["exit_code"] == 0 and not test["timed_out"]
                                   and not test["skipped"] for test in tests)
                if review_passed and tests_passed:
                    return WorkflowReport(True, changed, plan, summary, review, tests, cycle, diff)
            return WorkflowReport(False, changed, plan, summary, review, tests,
                                  max_corrections, diff, "Review or checks failed")
        except (ValueError, RuntimeError, OSError, UnicodeError):
            return WorkflowReport(False, changed, plan, summary, review, tests, 0, diff,
                                  "Specialist workflow failed safely")
        finally:
            workspace.close()
