"""Composio actions routed exclusively through Max's task engine."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from composio_adapter import ComposioAdapter, IntegrationError
from memory_store import MemoryStore
from task_engine import Orchestrator, Status, Task


_SECRET_KEYS = re.compile(r"(?:token|secret|password|api.?key|authorization|cookie|credential)", re.I)
_SECRET_VALUE = re.compile(r"(?i)(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}")


def redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if _SECRET_KEYS.search(str(k)) else redact(v, depth + 1))
                for k, v in list(value.items())[:100]}
    if isinstance(value, list):
        return [redact(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[redacted]", value[:8_000])
    return value if isinstance(value, (bool, int, float, type(None))) else str(value)[:200]


class ActionAudit:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS external_actions ("
                       "task_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, slug TEXT NOT NULL, "
                       "status TEXT NOT NULL, error_code TEXT, updated_at TEXT NOT NULL "
                       "DEFAULT CURRENT_TIMESTAMP)")

    def save(self, task: Task, account_id: str, slug: str) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO external_actions(task_id,account_id,slug,status,error_code) "
                       "VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                       "status=excluded.status,error_code=excluded.error_code,"
                       "updated_at=CURRENT_TIMESTAMP",
                       (task.task_id, account_id, slug, task.status.value, task.error_code))

    def get(self, task_id: str) -> dict | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT account_id,slug,status,error_code,updated_at "
                             "FROM external_actions WHERE task_id=?", (task_id,)).fetchone()
        return dict(zip(("account_id", "slug", "status", "error_code", "updated_at"), row)) if row else None


@dataclass(frozen=True)
class PreparedAction:
    task_id: str
    status: str
    result: Any = None
    error_code: str | None = None


class ExternalActionService:
    def __init__(self, adapter: ComposioAdapter, *, store_path: str, audit_path: str,
                 memory_path: str | None = None):
        self.adapter = adapter
        self.engine = Orchestrator(store_path=store_path)
        self.audit = ActionAudit(audit_path)
        self.memory = MemoryStore(memory_path or Path(audit_path).with_name("memories.sqlite3"))
        self._pending: dict[str, tuple[Task, dict, Any]] = {}
        self._register()

    def _register(self):
        for tool in ("composio.read", "composio.write"):
            async def handler(task, payload, expected=tool):
                action, actual = self.adapter.policy.decide(payload["account_id"], payload["slug"])
                if actual != expected or task.target != action.account_id:
                    raise IntegrationError("permission_denied")
                if expected == "composio.write":
                    await self.engine.revalidate_approval(task, payload)
                metadata, result = await self.adapter.execute(
                    action.account_id, action.slug, payload["arguments"])
                if metadata["tool"] != expected:
                    raise IntegrationError("permission_denied")
                if expected == "composio.read":
                    data = result if isinstance(result, dict) else getattr(result, "data", None)
                    return {"untrusted_external_data": redact(data)}
                return {"executed": True, "slug": action.slug}

            self.engine.register("external_" + tool.split(".")[-1], tool, handler,
                                 valid_input=lambda p: isinstance(p, dict) and set(p) == {
                                     "account_id", "slug", "arguments", "requester_id"
                                 } and isinstance(p["arguments"], dict),
                                 valid_result=lambda r: isinstance(r, dict))

    def _record(self, task: Task, account_id: str, slug: str) -> None:
        self.audit.save(task, account_id, slug)
        if task.status in {Status.COMPLETED, Status.FAILED, Status.INTERRUPTED, Status.CANCELLED}:
            outcome = "succeeded" if task.status == Status.COMPLETED else "failed"
            self.memory.feedback(task.task_id, outcome,
                                 f"{slug}: {task.error_code or task.status.value}")

    async def start(self, account_id: str, slug: str, arguments: dict, *, requester_id: str) -> PreparedAction:
        action, tool = self.adapter.policy.decide(account_id, slug)
        # Discovery and schema validation happen before constructing a task.
        metadata = await self.adapter.discover(account_id, slug)
        if metadata["tool"] != tool:
            raise IntegrationError("permission_denied")
        from composio_adapter import _validate
        if not _validate(arguments, metadata["schema"]):
            raise IntegrationError("invalid_arguments")
        payload = {"account_id": account_id, "slug": slug, "arguments": arguments,
                   "requester_id": requester_id}
        task = Task.new(f"Execute {slug}", "external_" + tool.split(".")[-1],
                        permitted_tools={tool}, target=account_id, timeout_seconds=90,
                        max_attempts=1)
        outcome = await self.engine.run(task, payload)
        self._record(outcome.task, account_id, slug)
        if outcome.task.status == Status.AWAITING_APPROVAL:
            self._pending[task.task_id] = (task, payload, outcome.approval_request)
        return PreparedAction(task.task_id, outcome.task.status.value, outcome.result,
                              outcome.task.error_code)

    async def decide(self, task_id: str, *, approved_by: str, approve: bool) -> PreparedAction:
        pending = self._pending.pop(task_id, None)
        if pending is None:
            raise IntegrationError("approval_unavailable")
        task, payload, request = pending
        if request.requester_id != approved_by:
            self._pending[task_id] = pending
            raise IntegrationError("permission_denied")
        if approve:
            await self.engine.approve(request, approved_by=approved_by)
            outcome = await self.engine.run(task, payload)
        else:
            denied = await self.engine.deny(request)
            self._record(denied, payload["account_id"], payload["slug"])
            return PreparedAction(task_id, denied.status.value, error_code=denied.error_code)
        self._record(outcome.task, payload["account_id"], payload["slug"])
        return PreparedAction(task_id, outcome.task.status.value, outcome.result,
                              outcome.task.error_code)

    async def cancel(self, task_id: str) -> PreparedAction:
        record = self.audit.get(task_id)
        if record is None:
            raise IntegrationError("unknown_task")
        task = await self.engine.cancel(task_id)
        self._pending.pop(task_id, None)
        self._record(task, record["account_id"], record["slug"])
        return PreparedAction(task_id, task.status.value, error_code=task.error_code)
