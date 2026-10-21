"""Bounded, in-process task orchestration with explicit capability approval.

Task records contain normalized descriptions and status only. Handler payloads and
full results are passed through the call, never retained in task history or logs.
The trusted caller must authenticate a human approval before calling ``approve``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Awaitable, Callable

from task_store import TaskStore


class Status(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ActionNotStarted(Exception):
    """Trusted handler certifies that no external action was attempted."""


class ApprovalStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class Permission(str, Enum):
    READ = "read_only"
    SANDBOX = "sandbox_execution"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    MESSAGE = "external_message"
    COMMERCE = "commerce"
    DEPLOY = "deployment"
    LOCAL_WRITE = "isolated_workspace_write"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    permission: Permission
    approval_required: bool = False
    target_required: bool = False


# An allowlist of capabilities, not executable functions. Phase 2 only wires model.reply.
TOOLS = MappingProxyType({
    spec.name: spec for spec in (
        ToolSpec("model.reply", "Query the configured model for a reply", Permission.READ),
        ToolSpec("repository.inspect", "Read repository metadata and files", Permission.READ),
        ToolSpec("workspace.edit", "Edit scoped files in an isolated workspace", Permission.LOCAL_WRITE),
        ToolSpec("workspace.review", "Review an isolated workspace diff", Permission.READ),
        ToolSpec("workspace.test", "Run allowlisted tests in an isolated workspace", Permission.SANDBOX),
        ToolSpec("sandbox.execute", "Run code in the existing sandbox", Permission.SANDBOX),
        ToolSpec("repository.stage", "Stage a task branch and manifest", Permission.WRITE, True, True),
        ToolSpec("repository.push", "Push code to a repository branch", Permission.WRITE, True, True),
        ToolSpec("repository.merge", "Merge a pull request", Permission.WRITE, True, True),
        ToolSpec("deployment.deploy", "Deploy an application", Permission.DEPLOY, True, True),
        ToolSpec("commerce.purchase", "Purchase an item", Permission.COMMERCE, True, True),
        ToolSpec("commerce.book", "Book a reservation", Permission.COMMERCE, True, True),
        ToolSpec("message.send", "Send an external message", Permission.MESSAGE, True, True),
        ToolSpec("user_data.write", "Modify saved user data", Permission.WRITE, True, True),
        ToolSpec("user_data.delete", "Delete saved user data", Permission.DESTRUCTIVE, True, True),
    )
})

ROLE_TOOLS = MappingProxyType({
    "planner": frozenset({"model.reply"}),
    "coder": frozenset({"workspace.edit"}),
    "reviewer": frozenset({"workspace.review"}),
    "tester": frozenset({"workspace.test"}),
})

MAX_ATTEMPTS = 3
MAX_TIMEOUT_SECONDS = 300
MAX_TASK_RECORDS = 256
_SENSITIVE = re.compile(
    r"\b(?:api[_-]?key|token|password|secret|authorization)\s*[:=]|"
    r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}", re.IGNORECASE
)
_TRANSITIONS = {
    Status.PENDING: {Status.RUNNING, Status.AWAITING_APPROVAL, Status.FAILED, Status.CANCELLED, Status.INTERRUPTED},
    Status.AWAITING_APPROVAL: {Status.PENDING, Status.FAILED, Status.CANCELLED, Status.INTERRUPTED},
    Status.RUNNING: {Status.PENDING, Status.COMPLETED, Status.FAILED, Status.CANCELLED, Status.INTERRUPTED},
    Status.COMPLETED: set(), Status.FAILED: set(), Status.CANCELLED: set(),
    Status.INTERRUPTED: set(),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_metadata(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{field} must be nonempty and at most 200 characters")
    if any(ord(char) < 32 for char in value) or _SENSITIVE.search(value):
        raise ValueError(f"{field} must not contain control characters or credentials")
    return value.strip()


@dataclass(frozen=True)
class Task:
    task_id: str
    description: str
    task_type: str
    status: Status
    created_at: datetime
    updated_at: datetime
    attempt: int
    max_attempts: int
    deadline_at: datetime
    requested_role: str | None
    permitted_tools: frozenset[str]
    target: str | None = None
    result_summary: str | None = None
    error_code: str | None = None
    approval_status: ApprovalStatus = ApprovalStatus.NOT_REQUIRED

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", self.task_id):
            raise ValueError("Invalid task ID")
        _safe_metadata(self.description, "description")
        _safe_metadata(self.task_type, "task_type")
        if self.target is not None:
            _safe_metadata(self.target, "target")
        if self.requested_role is not None:
            _safe_metadata(self.requested_role, "requested_role")
        if not isinstance(self.status, Status) or not isinstance(self.approval_status, ApprovalStatus):
            raise ValueError("Invalid task status")
        if (not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= MAX_ATTEMPTS
                or not isinstance(self.attempt, int) or not 0 <= self.attempt <= self.max_attempts):
            raise ValueError("Invalid attempt limit or count")
        if (not all(isinstance(value, datetime) for value in
                    (self.created_at, self.updated_at, self.deadline_at))
                or self.created_at.tzinfo is None or self.updated_at.tzinfo is None
                or self.deadline_at.tzinfo is None
                or not self.created_at <= self.updated_at
                or not 0 < (self.deadline_at - self.created_at).total_seconds() <= MAX_TIMEOUT_SECONDS):
            raise ValueError("Invalid task timestamps or deadline")
        if (not isinstance(self.permitted_tools, frozenset) or not self.permitted_tools
                or not all(isinstance(tool, str) for tool in self.permitted_tools)):
            raise ValueError("Invalid permitted tools")

    @classmethod
    def new(cls, description: str, task_type: str, *, permitted_tools: set[str] | frozenset[str],
            timeout_seconds: float = 60, max_attempts: int = 1,
            requested_role: str | None = None, target: str | None = None) -> Task:
        description = _safe_metadata(description, "description")
        task_type = _safe_metadata(task_type, "task_type")
        if target is not None:
            target = _safe_metadata(target, "target")
        if requested_role is not None:
            requested_role = _safe_metadata(requested_role, "requested_role")
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise ValueError("max_attempts is outside the supported range")
        if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds is outside the supported range")
        if not isinstance(permitted_tools, (set, frozenset)) or not permitted_tools:
            raise ValueError("permitted_tools must be a nonempty set")
        created = _now()
        return cls(uuid.uuid4().hex, description, task_type, Status.PENDING, created, created,
                   0, max_attempts, created + timedelta(seconds=timeout_seconds),
                   requested_role, frozenset(permitted_tools), target)

    def transition(self, status: Status, **changes: Any) -> Task:
        """Return a new record after an allowed state transition."""
        if not isinstance(status, Status) or status not in _TRANSITIONS[self.status]:
            raise ValueError(f"Invalid task transition from {self.status.value}")
        return replace(self, status=status, updated_at=_now(), **changes)


@dataclass(frozen=True)
class ApprovalRequest:
    task_id: str
    tool: str
    target: str
    action: str
    payload_digest: str
    requester_id: str = ""
    expires_at: datetime | None = None


@dataclass(frozen=True)
class Outcome:
    task: Task
    result: Any = None  # returned only to this caller; never retained in task history
    error: str | None = None
    approval_request: ApprovalRequest | None = None


Handler = Callable[[Task, Any], Awaitable[Any]]
Validator = Callable[[Any], bool]


@dataclass(frozen=True)
class _HandlerSpec:
    tool: str
    handler: Handler
    valid_input: Validator
    valid_result: Validator
    role: str | None = None


def _digest(payload: Any) -> str:
    """Bind an approval to a JSON-compatible action payload without storing it."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 64_000:
        raise ValueError("Action payload is too large")
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _task_record(task: Task) -> dict:
    """Persist only operational metadata; omit description, target, and payload."""
    return {
        "schema": 1, "task_id": task.task_id, "task_type": task.task_type,
        "status": task.status.value, "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(), "deadline_at": task.deadline_at.isoformat(),
        "attempt": task.attempt, "max_attempts": task.max_attempts,
        "permitted_tools": sorted(task.permitted_tools),
        "error_code": task.error_code if task.error_code and re.fullmatch(r"[a-z_]{1,40}", task.error_code) else None,
        "result_summary": "Result available" if task.result_summary else None,
    }


def _restore_task(record: dict) -> Task | None:
    try:
        if set(record) != {"schema", "task_id", "task_type", "status", "created_at",
                           "updated_at", "deadline_at", "attempt", "max_attempts",
                           "permitted_tools", "error_code", "result_summary"}:
            return None
        if record["schema"] != 1 or type(record["attempt"]) is not int or type(record["max_attempts"]) is not int:
            return None
        tools = record["permitted_tools"]
        if not isinstance(tools, list) or not tools or len(tools) > len(TOOLS) or any(
            not isinstance(name, str) or name not in TOOLS for name in tools
        ):
            return None
        error = record["error_code"]
        if error is not None and (not isinstance(error, str) or not re.fullmatch(r"[a-z_]{1,40}", error)):
            return None
        if record["result_summary"] not in (None, "Result available"):
            return None
        task = Task(
            record["task_id"], "Recovered task", record["task_type"],
            Status(record["status"]), datetime.fromisoformat(record["created_at"]),
            datetime.fromisoformat(record["updated_at"]), record["attempt"],
            record["max_attempts"], datetime.fromisoformat(record["deadline_at"]),
            None, frozenset(tools), result_summary=record["result_summary"],
            error_code=error,
        )
        if task.status in (Status.PENDING, Status.RUNNING, Status.AWAITING_APPROVAL):
            consequential = any(TOOLS[name].approval_required for name in tools)
            if task.status == Status.AWAITING_APPROVAL:
                code = "approval_expired"
            elif task.status == Status.RUNNING and consequential:
                code = "outcome_uncertain"
            else:
                code = "interrupted"
            task = task.transition(Status.INTERRUPTED, error_code=code,
                                   approval_status=ApprovalStatus.NOT_REQUIRED)
        return task
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


class Orchestrator:
    def __init__(self, *, store_path: str | None = None) -> None:
        self._handlers: dict[str, _HandlerSpec] = {}
        self._tasks: dict[str, Task] = {}
        self._submitted: dict[str, Task] = {}
        self._approval_requests: dict[str, ApprovalRequest] = {}
        self._approved: set[str] = set()
        self._approvers: dict[str, str] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._store = TaskStore(store_path) if store_path else None
        self._storage_failed = bool(self._store and not self._store.available)
        self._recovered_ids: set[str] = set()
        if self._store and not self._storage_failed:
            for record in self._store.load():
                restored = _restore_task(record)
                if restored is None:
                    continue
                self._tasks[restored.task_id] = restored
                self._submitted[restored.task_id] = restored
                self._recovered_ids.add(restored.task_id)
                if restored.status == Status.INTERRUPTED and record["status"] != Status.INTERRUPTED.value:
                    self._store.save(_task_record(restored))
            self._storage_failed = not self._store.available

    def _remember(self, task: Task) -> bool:
        self._tasks[task.task_id] = task
        if self._store and not self._store.save(_task_record(task)):
            self._storage_failed = True
            return False
        return True

    def _expire_stale(self, task: Task) -> Task:
        if task.status in (Status.PENDING, Status.AWAITING_APPROVAL) and _now() >= task.deadline_at:
            task = task.transition(
                Status.FAILED, error_code="timeout",
                approval_status=(ApprovalStatus.DENIED
                                 if task.approval_status != ApprovalStatus.NOT_REQUIRED
                                 else task.approval_status),
            )
            self._remember(task)
            self._approval_requests.pop(task.task_id, None)
            self._approved.discard(task.task_id)
            self._approvers.pop(task.task_id, None)
        return task

    def register(self, task_type: str, tool: str, handler: Handler, *,
                 valid_input: Validator, valid_result: Validator,
                 role: str | None = None) -> None:
        if tool not in TOOLS or task_type in self._handlers:
            raise ValueError("Unknown tool or duplicate task type")
        if role is not None and (role not in ROLE_TOOLS or tool not in ROLE_TOOLS[role]):
            raise ValueError("Tool is not permitted for this role")
        self._handlers[task_type] = _HandlerSpec(tool, handler, valid_input, valid_result, role)

    def get(self, task_id: str) -> Task | None:
        task = self._tasks.get(task_id)
        return self._expire_stale(task) if task else None

    async def run(self, task: Task, payload: Any = None) -> Outcome:
        async with self._lock:
            if self._storage_failed:
                return Outcome(task, error="Task state storage is unavailable.")
            if task.task_id in self._recovered_ids:
                return Outcome(self._tasks[task.task_id],
                               error="Recovered task requires reconciliation or a new request.")
            prior = self._submitted.get(task.task_id)
            if prior is not None and prior != task:
                raise ValueError("Task ID already belongs to another request")
            if prior is None:
                if (task.status != Status.PENDING or task.attempt != 0
                        or task.approval_status != ApprovalStatus.NOT_REQUIRED
                        or task.result_summary is not None or task.error_code is not None):
                    raise ValueError("New tasks must begin pending with zero attempts")
                while len(self._tasks) >= MAX_TASK_RECORDS:
                    for old_id, old in self._tasks.items():
                        if old.status in (Status.COMPLETED, Status.FAILED, Status.CANCELLED, Status.INTERRUPTED):
                            if self._store and not self._store.delete(old_id):
                                self._storage_failed = True
                                return Outcome(task, error="Task state storage is unavailable.")
                            self._tasks.pop(old_id)
                            self._submitted.pop(old_id)
                            self._recovered_ids.discard(old_id)
                            self._approval_requests.pop(old_id, None)
                            self._approved.discard(old_id)
                            self._approvers.pop(old_id, None)
                            break
                    else:
                        return Outcome(task, error="Task engine is busy.")
                self._submitted[task.task_id] = task
                if not self._remember(task):
                    return Outcome(task, error="Task state storage is unavailable.")
            current = self._expire_stale(self._tasks[task.task_id])
            if current.status != Status.PENDING:
                request = self._approval_requests.get(task.task_id)
                return Outcome(current, error=("Task deadline expired." if current.error_code == "timeout"
                                               else "Task already started."),
                               approval_request=request if current.status == Status.AWAITING_APPROVAL else None)

            try:
                payload_digest = _digest(payload)
            except (TypeError, ValueError):
                return self._fail(current, "invalid_input", "Task input must be JSON-compatible.")

            spec = self._handlers.get(task.task_type)
            if spec is None:
                return self._fail(current, "unsupported_task", "Unsupported task type.")
            tool = TOOLS[spec.tool]
            if (task.requested_role != spec.role or not task.permitted_tools.issubset(TOOLS)
                    or (spec.role is not None and not task.permitted_tools.issubset(ROLE_TOOLS[spec.role]))
                    or spec.tool not in task.permitted_tools):
                return self._fail(current, "permission_denied", "Task capability is not permitted.")
            if tool.target_required and not task.target:
                return self._fail(current, "missing_target", "This action needs a target.")
            try:
                valid = spec.valid_input(payload)
            except Exception:
                valid = False
            if not valid:
                return self._fail(current, "invalid_input", "Task input is invalid.")

            if tool.approval_required:
                request = self._approval_requests.get(task.task_id)
                if task.task_id not in self._approved:
                    request = ApprovalRequest(task.task_id, spec.tool, task.target or "",
                                              task.description, payload_digest,
                                              str(payload.get("requester_id", "")) if isinstance(payload, dict) else "",
                                              min(task.deadline_at, _now() + timedelta(minutes=5)))
                    self._approval_requests[task.task_id] = request
                    current = current.transition(Status.AWAITING_APPROVAL,
                                                 approval_status=ApprovalStatus.PENDING)
                    if not self._remember(current):
                        interrupted = current.transition(Status.INTERRUPTED, error_code="storage_unavailable")
                        self._tasks[task.task_id] = interrupted
                        return Outcome(interrupted, error="Task state storage is unavailable.")
                    return Outcome(current, approval_request=request)
                if (request is None or request.payload_digest != payload_digest
                        or request.expires_at is None or _now() >= request.expires_at):
                    return self._fail(current, "approval_scope_mismatch", "Approval does not cover this action.")

            current = current.transition(Status.RUNNING, attempt=current.attempt + 1)
            if not self._remember(current):
                interrupted = current.transition(Status.INTERRUPTED, error_code="storage_unavailable")
                self._tasks[task.task_id] = interrupted
                return Outcome(interrupted, error="Task state storage is unavailable.")
            self._inflight[task.task_id] = asyncio.current_task()

        try:
            while True:
                remaining = (current.deadline_at - _now()).total_seconds()
                if remaining <= 0:
                    return await self._finish_failure(task.task_id, "timeout", "Task timed out.")
                try:
                    result = await asyncio.wait_for(spec.handler(current, payload), timeout=remaining)
                    if not spec.valid_result(result):
                        raise ValueError("Invalid handler result")
                    async with self._lock:
                        current = self._tasks[task.task_id]
                        if current.status in (Status.CANCELLED, Status.INTERRUPTED):
                            return Outcome(current, error="Task stopped; outcome may be uncertain.")
                        current = current.transition(Status.COMPLETED, result_summary="Result available")
                        if not self._remember(current):
                            interrupted = replace(current, status=Status.INTERRUPTED,
                                                  updated_at=_now(), result_summary=None,
                                                  error_code="outcome_uncertain")
                            self._tasks[task.task_id] = interrupted
                            return Outcome(interrupted, error="Task outcome is uncertain; storage failed.")
                        return Outcome(current, result=result)
                except asyncio.TimeoutError:
                    return await self._finish_failure(task.task_id, "timeout", "Task timed out.")
                except asyncio.CancelledError:
                    async with self._lock:
                        current = self._tasks[task.task_id]
                        if current.status not in (Status.CANCELLED, Status.INTERRUPTED):
                            consequential = TOOLS[spec.tool].approval_required
                            current = current.transition(
                                Status.INTERRUPTED if consequential else Status.CANCELLED,
                                error_code="outcome_uncertain" if consequential else None)
                            self._remember(current)
                        return Outcome(current, error="Task stopped; underlying work may continue.")
                except ActionNotStarted:
                    async with self._lock:
                        current = self._tasks[task.task_id]
                        if current.status in (Status.CANCELLED, Status.INTERRUPTED):
                            return Outcome(current, error="Task stopped.")
                        return self._fail(current, "action_not_started", "External action was not started.")
                except Exception:
                    async with self._lock:
                        current = self._tasks[task.task_id]
                        if current.status in (Status.CANCELLED, Status.INTERRUPTED):
                            return Outcome(current, error="Task stopped; outcome may be uncertain.")
                        if tool.approval_required:
                            current = current.transition(Status.INTERRUPTED, error_code="outcome_uncertain")
                            self._remember(current)
                            return Outcome(current, error="External action outcome is uncertain; reconcile before retrying.")
                        if current.attempt >= current.max_attempts:
                            return self._fail(current, "handler_error", "Task execution failed.")
                        current = current.transition(Status.PENDING).transition(
                            Status.RUNNING, attempt=current.attempt + 1)
                        if not self._remember(current):
                            interrupted = current.transition(Status.INTERRUPTED, error_code="storage_unavailable")
                            self._tasks[task.task_id] = interrupted
                            return Outcome(interrupted, error="Task state storage is unavailable.")
        finally:
            async with self._lock:
                self._inflight.pop(task.task_id, None)

    def _fail(self, task: Task, code: str, message: str) -> Outcome:
        failed = task.transition(Status.FAILED, error_code=code)
        self._remember(failed)
        return Outcome(failed, error=message)

    async def _finish_failure(self, task_id: str, code: str, message: str) -> Outcome:
        async with self._lock:
            current = self._tasks[task_id]
            if current.status in (Status.CANCELLED, Status.INTERRUPTED):
                return Outcome(current, error="Task stopped; outcome may be uncertain.")
            if any(TOOLS[name].approval_required for name in current.permitted_tools if name in TOOLS):
                interrupted = current.transition(Status.INTERRUPTED, error_code="outcome_uncertain")
                self._remember(interrupted)
                return Outcome(interrupted, error="External action outcome is uncertain; reconcile before retrying.")
            return self._fail(current, code, message)

    async def approve(self, request: ApprovalRequest, *, approved_by: str) -> Task:
        """Called only after a trusted interface verifies explicit human approval."""
        _safe_metadata(approved_by, "approved_by")
        async with self._lock:
            current = self.get(request.task_id)
            if (current is None or current.status != Status.AWAITING_APPROVAL
                    or self._approval_requests.get(request.task_id) != request
                    or (request.expires_at is not None and _now() >= request.expires_at)
                    or (request.requester_id and request.requester_id != approved_by)):
                raise ValueError("Approval does not match a pending action")
            current = current.transition(Status.PENDING,
                                         approval_status=ApprovalStatus.APPROVED)
            if not self._remember(current):
                raise ValueError("Task state storage is unavailable")
            self._approved.add(request.task_id)
            self._approvers[request.task_id] = approved_by
            return current

    async def revalidate_approval(self, task: Task, payload: Any) -> None:
        """Check the exact running action again directly before an external write."""
        async with self._lock:
            current = self._tasks.get(task.task_id)
            request = self._approval_requests.get(task.task_id)
            spec = self._handlers.get(task.task_type)
            try:
                digest = _digest(payload)
            except (TypeError, ValueError):
                digest = None
            if (current is None or current.status != Status.RUNNING
                    or current.task_type != task.task_type or current.target != task.target
                    or spec is None or not TOOLS[spec.tool].approval_required
                    or request is None or request.tool != spec.tool
                    or request.target != task.target or request.action != task.description
                    or request.payload_digest != digest
                    or request.expires_at is None or _now() >= request.expires_at
                    or task.task_id not in self._approved
                    or task.task_id not in self._approvers
                    or (request.requester_id and self._approvers[task.task_id] != request.requester_id)):
                raise ValueError("Approval is no longer valid for this write")

    async def deny(self, request: ApprovalRequest) -> Task:
        async with self._lock:
            current = self._tasks.get(request.task_id)
            if (current is None or current.status != Status.AWAITING_APPROVAL
                    or self._approval_requests.get(request.task_id) != request):
                raise ValueError("Denial does not match a pending action")
            current = current.transition(Status.FAILED, error_code="approval_denied",
                                         approval_status=ApprovalStatus.DENIED)
            self._remember(current)
            return current

    async def cancel(self, task_id: str) -> Task:
        async with self._lock:
            current = self._tasks[task_id]
            if current.status in (Status.COMPLETED, Status.FAILED, Status.CANCELLED, Status.INTERRUPTED):
                return current
            consequential_running = (
                current.status == Status.RUNNING
                and any(TOOLS[name].approval_required for name in current.permitted_tools if name in TOOLS)
            )
            current = current.transition(
                Status.INTERRUPTED if consequential_running else Status.CANCELLED,
                error_code="outcome_uncertain" if consequential_running else None)
            self._remember(current)
            runner = self._inflight.get(task_id)
            if runner is not None:
                runner.cancel()
            return current
