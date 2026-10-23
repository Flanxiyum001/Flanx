"""Single-use Discord approval and test-repository pull request publishing.

Only non-sensitive operation metadata is durable. Proposed source stays in memory;
restart invalidates approval and never resumes a GitHub write automatically.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Callable

from specialists import MAX_DIFF_BYTES, review_diff
from authz import parse_discord_ids
from task_engine import ActionNotStarted, Orchestrator, Status, Task, _digest


_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_BRANCH = re.compile(r"max-fix-[A-Za-z0-9-]+\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_PATH_PART = re.compile(r"[A-Za-z0-9_.-]+\Z")
_CREDENTIAL = re.compile(r"(?i)\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*['\"]?[^\s'\"]{8,}|"
                         r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}")
_STATES = frozenset({"prepared", "awaiting_approval", "approved", "executing", "expired",
                     "completed", "failed", "outcome_uncertain", "reconciliation_required",
                     "rejected"})


def _ids(raw: str) -> frozenset[int]:
    return parse_discord_ids(raw)[0]


@dataclass(frozen=True)
class PublishConfig:
    enabled: bool
    test_repo: str
    approver_ids: frozenset[int]
    token: str = field(repr=False)
    store_path: str

    @classmethod
    def from_env(cls, env=None) -> PublishConfig:
        env = os.environ if env is None else env
        return cls(env.get("MAX_PUBLISH_ENABLED") == "true",
                   env.get("MAX_PUBLISH_TEST_REPO", "").strip(),
                   _ids(env.get("MAX_APPROVER_DISCORD_IDS", "")),
                   env.get("MAX_GITHUB_PUBLISH_TOKEN", "").strip(),
                   env.get("MAX_PUBLISH_DB") or str(Path(__file__).parent / ".max_state" / "publishing.sqlite3"))

    @property
    def ready(self) -> bool:
        return (self.enabled and bool(_REPO.fullmatch(self.test_repo))
                and all(part not in (".", "..") for part in self.test_repo.split("/"))
                and bool(self.token) and bool(self.store_path))


class OperationStore:
    """Durable metadata ledger. A storage failure closes the publishing gate."""

    def __init__(self, path: str):
        self.available = True
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS publish_operations "
                           "(task_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        except (OSError, sqlite3.Error):
            self.available = False

    def save(self, record: dict) -> bool:
        if not self.available:
            return False
        try:
            record = {**record, "updated_at": datetime.now(timezone.utc).isoformat()}
            raw = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(raw) > 4000:
                return False
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("INSERT INTO publish_operations VALUES (?, ?) "
                           "ON CONFLICT(task_id) DO UPDATE SET record=excluded.record",
                           (record["task_id"], raw))
            return True
        except (OSError, sqlite3.Error, TypeError, ValueError, KeyError):
            self.available = False
            return False

    def load(self, task_id: str) -> dict | None:
        if not self.available:
            return None
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                row = db.execute("SELECT record FROM publish_operations WHERE task_id=?", (task_id,)).fetchone()
            record = json.loads(row[0]) if row else None
            return record if (isinstance(record, dict) and record.get("task_id") == task_id
                              and record.get("state") in _STATES) else None
        except (OSError, sqlite3.Error, TypeError, ValueError):
            self.available = False
            return None


@dataclass(frozen=True)
class Prepared:
    task_id: str
    nonce: str
    summary: str
    expires_at: datetime


class PublishError(Exception):
    """Safe reason code; never contains source, token, or provider response."""


class PublishingCoordinator:
    def __init__(self, config: PublishConfig, github_factory: Callable[[], object],
                 *, engine: Orchestrator | None = None, store: OperationStore | None = None):
        self.config = config
        self.github_factory = github_factory
        self.engine = engine or Orchestrator(store_path=str(Path(config.store_path).with_name("publish_tasks.sqlite3")))
        self.store = store or OperationStore(config.store_path)
        self._pending: dict[str, tuple[Task, dict, str]] = {}
        self._lock = asyncio.Lock()
        self.engine.register("approved_test_pr", "repository.push", self._execute,
                             valid_input=self._valid_payload,
                             valid_result=lambda value: isinstance(value, str) and value.startswith("https://github.com/"))

    def _valid_payload(self, payload) -> bool:
        return (isinstance(payload, dict)
                and set(payload) == {"repo", "base_branch", "branch", "file", "content",
                                     "requester_id", "base_sha", "file_sha",
                                     "goal_sha256", "validation_sha256",
                                     "review_sha256", "review_summary"}
                and all(isinstance(v, str) for v in payload.values())
                and payload["repo"] == self.config.test_repo
                and bool(_BRANCH.fullmatch(payload["branch"]))
                and bool(_SHA.fullmatch(payload["base_sha"]))
                and bool(_SHA.fullmatch(payload["file_sha"]))
                and bool(re.fullmatch(r"[0-9a-f]{64}", payload["goal_sha256"]))
                and bool(re.fullmatch(r"[0-9a-f]{64}", payload["validation_sha256"]))
                and bool(re.fullmatch(r"[0-9a-f]{64}", payload["review_sha256"]))
                and bool(payload["content"]) and len(payload["content"].encode()) <= 60_000
                and not _CREDENTIAL.search(payload["content"])
                and bool(re.fullmatch(r"[1-9][0-9]{0,19}", payload["requester_id"]))
                and bool(re.fullmatch(r"[A-Za-z0-9._/-]+", payload["base_branch"]))
                and len(payload["base_branch"]) <= 100
                and all(part not in ("", ".", "..") for part in payload["base_branch"].split("/"))
                and bool(payload["file"]) and not payload["file"].startswith("/")
                and "\\" not in payload["file"]
                and all(_PATH_PART.fullmatch(part) and part not in (".", "..", ".git", ".env")
                        for part in payload["file"].split("/")))

    def _client(self):
        if not self.config.ready or not self.store.available or self.engine._storage_failed:
            raise PublishError("publishing_not_configured")
        try:
            client = self.github_factory()
        except Exception:
            raise PublishError("github_unavailable") from None
        if client is None:
            raise PublishError("github_unavailable")
        return client

    async def prepare(self, *, repo: str, base: str, branch: str, file: str,
                      original: str, content: str, goal: str, validation: str,
                      validation_passed: bool, requester_id: int) -> Prepared:
        if not self.config.ready:
            raise PublishError("publishing_not_configured")
        if validation_passed is not True:
            raise PublishError("validation_not_passed")
        if repo != self.config.test_repo:
            raise PublishError("unauthorized_target_or_user")
        if not _BRANCH.fullmatch(branch):
            raise PublishError("invalid_branch")
        client = self._client()

        def inspect():
            target = client.get_repo(repo)
            if target.full_name != repo or target.default_branch != base:
                raise PublishError("repository_changed")
            base_sha = target.get_branch(base).commit.sha
            item = target.get_contents(file, ref=base)
            if isinstance(item, list) or item.decoded_content.decode("utf-8") != original:
                raise PublishError("source_changed")
            return base_sha, item.sha

        try:
            base_sha, file_sha = await asyncio.to_thread(inspect)
        except PublishError:
            raise
        except Exception:
            raise PublishError("github_unavailable") from None
        diff = "".join(difflib.unified_diff(original.splitlines(keepends=True),
                                            content.splitlines(keepends=True)))
        if not diff or len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
            raise PublishError("diff_invalid_or_too_large")
        review = await review_diff({"diff": diff})
        high = sum(item["severity"] == "high" for item in review["findings"])
        medium = sum(item["severity"] == "medium" for item in review["findings"])
        if high:
            raise PublishError("static_review_blocked")
        review_summary = f"Static review: {high} high, {medium} medium findings"
        payload = {"repo": repo, "base_branch": base, "branch": branch, "file": file,
                   "content": content, "requester_id": str(requester_id),
                   "base_sha": base_sha, "file_sha": file_sha,
                   "goal_sha256": hashlib.sha256(goal.encode()).hexdigest(),
                   "validation_sha256": hashlib.sha256(validation.encode()).hexdigest(),
                   "review_sha256": review["diff_sha256"], "review_summary": review_summary}
        if not self._valid_payload(payload):
            raise PublishError("invalid_proposal")
        task = Task.new("Create test branch and pull request", "approved_test_pr",
                        permitted_tools={"repository.push"}, target=f"{repo}:{branch}:{file}",
                        timeout_seconds=300)
        nonce = secrets.token_urlsafe(24)
        digest = _digest(payload)
        changes = list(difflib.ndiff(original.splitlines(), content.splitlines()))
        added = sum(line.startswith("+ ") for line in changes)
        removed = sum(line.startswith("- ") for line in changes)
        record = {"task_id": task.task_id, "state": "prepared", "repo": repo,
                  "base": base, "branch": branch, "file": file, "requester_id": str(requester_id),
                  "payload_digest": digest, "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                  "base_sha": base_sha, "file_sha": file_sha,
                  "expires_at": task.deadline_at.isoformat(), "result_url": None}
        async with self._lock:
            if not self.store.save(record):
                raise PublishError("storage_unavailable")
            outcome = await self.engine.run(task, payload)
            if outcome.task.status != Status.AWAITING_APPROVAL or not outcome.approval_request:
                record["state"] = "failed"
                self.store.save(record)
                raise PublishError("preparation_failed")
            record["state"] = "awaiting_approval"
            if not self.store.save(record):
                await self.engine.cancel(task.task_id)
                raise PublishError("storage_unavailable")
            self._pending[task.task_id] = (task, payload, nonce)
            asyncio.create_task(self._expire_later(task.task_id, task.deadline_at))
        summary = (f"**Max approval request** — task `{task.task_id}`\n"
                   f"Requester: <@{requester_id}>\n"
                   f"Operation: create one test branch, commit `{file}`, open one pull request.\n"
                   f"Repository: `{repo}` · base: `{base}` · head: `{branch}`\n"
                   f"Changed files: `{file}` (+{added}/-{removed} lines). "
                   f"Payload SHA-256: `{digest[:16]}`.\n"
                   f"Tests: sandbox validation passed. {review_summary}.\n"
                   f"Expires: <t:{int(task.deadline_at.timestamp())}:F>.\n"
                   "Approving publishes the proposed file on the head branch and opens a PR; "
                   "it does not merge or deploy.")
        return Prepared(task.task_id, nonce, summary, task.deadline_at)

    async def _expire_later(self, task_id: str, deadline: datetime) -> None:
        try:
            await asyncio.sleep(max(0, (deadline - datetime.now(timezone.utc)).total_seconds()))
            async with self._lock:
                if task_id not in self._pending:
                    return
                self._pending.pop(task_id, None)
                record = self.store.load(task_id)
                if record is not None and record.get("state") == "awaiting_approval":
                    self.store.save({**record, "state": "expired"})
                await self.engine.cancel(task_id)
        except asyncio.CancelledError:
            return

    async def decide(self, task_id: str, nonce: str, user_id: int, *, approve: bool) -> str:
        async with self._lock:
            if not self.config.ready:
                return "unauthorized"
            pending = self._pending.get(task_id)
            record = self.store.load(task_id)
            if pending is None or record is None or not self.store.available:
                return "unavailable"
            task, payload, expected_nonce = pending
            if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected_nonce):
                return "mismatched_request"
            try:
                digest = _digest(payload)
                content_digest = hashlib.sha256(payload["content"].encode()).hexdigest()
            except (KeyError, TypeError, ValueError, UnicodeError):
                return "mismatched_request"
            if (record.get("state") != "awaiting_approval"
                    or record.get("repo") != payload["repo"] or record.get("branch") != payload["branch"]
                    or record.get("base") != payload["base_branch"]
                    or record.get("payload_digest") != digest
                    or record.get("content_sha256") != content_digest):
                return "mismatched_request"
            current = self.engine.get(task_id)
            if current is None or current.status != Status.AWAITING_APPROVAL:
                return "stale_request"
            if datetime.now(timezone.utc) >= task.deadline_at:
                return "expired"
            request = self.engine._approval_requests.get(task_id)
            if request is None or request.payload_digest != record["payload_digest"]:
                return "mismatched_request"
            if not approve:
                if not self.store.save({**record, "state": "rejected", "decided_by": str(user_id)}):
                    return "storage_unavailable"
                try:
                    await self.engine.deny(request)
                except ValueError:
                    return "stale_request"
                self._pending.pop(task_id, None)
                return "rejected"
            if not self.store.save({**record, "state": "approved", "decided_by": str(user_id)}):
                return "storage_unavailable"
            try:
                await self.engine.approve(request, approved_by=str(user_id))
            except ValueError:
                return "stale_request"
            # Remove the interactive nonce before any external write. Replays fail closed.
            self._pending.pop(task_id, None)
        record = {**record, "decided_by": str(user_id)}
        if not self.store.save({**record, "state": "executing"}):
            return "storage_unavailable"
        outcome = await self.engine.run(task, payload)
        if outcome.task.status == Status.COMPLETED:
            if not self.store.save({**record, "state": "completed", "result_url": outcome.result}):
                return "outcome_uncertain"
            return "completed"
        if outcome.task.error_code in ("action_not_started", "approval_scope_mismatch", "invalid_input"):
            self.store.save({**record, "state": "failed"})
            return "failed"
        self.store.save({**record, "state": "outcome_uncertain"})
        return "outcome_uncertain"

    async def _execute(self, task: Task, payload: dict) -> str:
        record = self.store.load(task.task_id)
        if (record is None or record.get("state") != "executing" or not self._valid_payload(payload)
                or record.get("payload_digest") != _digest(payload)
                or record.get("repo") != self.config.test_repo
                or record.get("branch") != payload["branch"]
                or record.get("requester_id") != payload["requester_id"]):
            raise ActionNotStarted("Approved proposal changed")
        client = self._client()

        def inspect():
            repo = client.get_repo(payload["repo"])
            if repo.full_name != payload["repo"] or repo.default_branch != payload["base_branch"]:
                raise ActionNotStarted("Repository target changed")
            if repo.get_branch(payload["base_branch"]).commit.sha != payload["base_sha"]:
                raise ActionNotStarted("Base branch changed")
            file_obj = repo.get_contents(payload["file"], ref=payload["base_branch"])
            if isinstance(file_obj, list) or file_obj.sha != payload["file_sha"]:
                raise ActionNotStarted("Source changed")
            try:
                repo.get_branch(payload["branch"])
            except Exception as exc:
                if getattr(exc, "status", None) != 404:
                    raise
            else:
                raise ActionNotStarted("Head branch already exists")
            return repo

        try:
            repo = await asyncio.to_thread(inspect)
        except ActionNotStarted:
            raise
        except Exception:
            raise ActionNotStarted("GitHub preflight unavailable") from None
        await self.engine.revalidate_approval(task, payload)

        def write():
            repo.create_git_ref(ref=f'refs/heads/{payload["branch"]}', sha=payload["base_sha"])
            repo.update_file(path=payload["file"], message=f'Max approved patch {task.task_id[:12]}',
                             content=payload["content"], sha=payload["file_sha"], branch=payload["branch"])
            pull = repo.create_pull(title=f'Max approved patch: {payload["file"]}',
                                    body=(f'Approved Max task `{task.task_id}`. '
                                          f'File: `{payload["file"]}`. Sandbox validation passed. '
                                          f'{payload["review_summary"]}.'),
                                    head=payload["branch"], base=payload["base_branch"])
            return pull.html_url

        return await asyncio.to_thread(write)

    async def reconcile(self, task_id: str) -> str:
        """Read GitHub only. Never resumes a write or reuses an old approval."""
        record = self.store.load(task_id)
        if record is None or not self.store.available:
            return "storage_unavailable"
        if record.get("repo") != self.config.test_repo:
            return "repository_mismatch"
        if record.get("state") not in ("executing", "outcome_uncertain", "reconciliation_required"):
            return "not_uncertain"
        current = self.engine.get(task_id)
        if record["state"] == "executing" and current is not None and current.status == Status.RUNNING:
            return "executing"
        try:
            client = self._client()

            def inspect():
                repo = client.get_repo(record["repo"])
                branch = repo.get_branch(record["branch"])
                item = repo.get_contents(record["file"], ref=record["branch"])
                content_ok = (not isinstance(item, list) and
                              hashlib.sha256(item.decoded_content).hexdigest() == record["content_sha256"])
                pulls = repo.get_pulls(state="all", head=f'{record["repo"].split("/")[0]}:{record["branch"]}',
                                       base=record["base"])
                match = next((p for p in islice(pulls, 20) if p.head.ref == record["branch"]
                              and p.head.repo.full_name == record["repo"]
                              and p.base.ref == record["base"]
                              and [f.filename for f in p.get_files()] == [record["file"]]), None)
                return bool(branch), content_ok, match

            branch, content_ok, pull = await asyncio.to_thread(inspect)
        except Exception:
            self.store.save({**record, "state": "reconciliation_required"})
            return "reconciliation_required"
        if branch and content_ok and pull is not None:
            if self.store.save({**record, "state": "completed", "result_url": pull.html_url}):
                return "completed"
            return "storage_unavailable"
        self.store.save({**record, "state": "reconciliation_required"})
        return "reconciliation_required"
