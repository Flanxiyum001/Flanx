"""Durable metadata and conservative recovery tests."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from task_engine import Orchestrator, Status, Task


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tasks.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    async def test_persist_reload_without_prompt_or_result(self):
        engine = Orchestrator(store_path=str(self.path))
        calls = 0

        async def reply(_task, _payload):
            nonlocal calls
            calls += 1
            return "private model response"

        engine.register("reply", "model.reply", reply,
                        valid_input=lambda value: isinstance(value, str),
                        valid_result=lambda value: isinstance(value, str))
        task = Task.new("Private project description", "reply",
                        permitted_tools={"model.reply"})
        outcome = await engine.run(task, "private prompt and source")
        self.assertEqual(outcome.task.status, Status.COMPLETED)
        raw = self.path.read_bytes()
        for sensitive in (b"Private project description", b"private prompt",
                          b"private model response", b"source"):
            self.assertNotIn(sensitive, raw)
        recovered = Orchestrator(store_path=str(self.path))
        self.assertEqual(recovered.get(task.task_id).status, Status.COMPLETED)
        duplicate = await recovered.run(task, "private prompt and source")
        self.assertEqual(duplicate.task.status, Status.COMPLETED)
        self.assertEqual(calls, 1)

    async def test_running_read_task_becomes_interrupted(self):
        engine = Orchestrator(store_path=str(self.path))
        started = asyncio.Event()
        blocker = asyncio.Event()

        async def read(_task, _payload):
            started.set()
            await blocker.wait()
            return "done"

        engine.register("read", "repository.inspect", read,
                        valid_input=lambda value: value is None,
                        valid_result=lambda value: isinstance(value, str))
        task = Task.new("Inspect", "read", permitted_tools={"repository.inspect"})
        runner = asyncio.create_task(engine.run(task))
        await started.wait()
        recovered = Orchestrator(store_path=str(self.path))
        state = recovered.get(task.task_id)
        self.assertEqual((state.status, state.error_code), (Status.INTERRUPTED, "interrupted"))
        self.assertEqual((await recovered.run(task)).task.status, Status.INTERRUPTED)
        await engine.cancel(task.task_id)
        await runner

    async def test_uncertain_external_action_is_never_replayed(self):
        engine = Orchestrator(store_path=str(self.path))
        started = asyncio.Event()
        blocker = asyncio.Event()
        calls = 0

        async def external(_task, _payload):
            nonlocal calls
            calls += 1
            started.set()
            await blocker.wait()
            return "done"

        engine.register("write", "repository.push", external,
                        valid_input=lambda value: isinstance(value, dict),
                        valid_result=lambda value: value == "done")
        task = Task.new("Push branch", "write", permitted_tools={"repository.push"},
                        target="owner/repo:branch", max_attempts=3)
        payload = {"commit": "abc"}
        pending = await engine.run(task, payload)
        await engine.approve(pending.approval_request, approved_by="owner")
        runner = asyncio.create_task(engine.run(task, payload))
        await started.wait()
        recovered = Orchestrator(store_path=str(self.path))
        state = recovered.get(task.task_id)
        self.assertEqual((state.status, state.error_code),
                         (Status.INTERRUPTED, "outcome_uncertain"))
        duplicate = await recovered.run(task, payload)
        self.assertEqual(duplicate.task.status, Status.INTERRUPTED)
        with self.assertRaises(ValueError):
            await recovered.approve(pending.approval_request, approved_by="owner")
        self.assertEqual(calls, 1)
        cancelled = await engine.cancel(task.task_id)
        self.assertEqual(cancelled.status, Status.INTERRUPTED)
        await runner

    async def test_pending_approval_expires_on_restart(self):
        engine = Orchestrator(store_path=str(self.path))
        engine.register("write", "repository.stage", lambda *_: None,
                        valid_input=lambda value: isinstance(value, dict),
                        valid_result=lambda value: True)
        task = Task.new("Stage branch", "write", permitted_tools={"repository.stage"},
                        target="owner/repo:branch")
        pending = await engine.run(task, {"manifest": "private source"})
        recovered = Orchestrator(store_path=str(self.path))
        self.assertEqual(recovered.get(task.task_id).status, Status.INTERRUPTED)
        self.assertEqual(recovered.get(task.task_id).error_code, "approval_expired")
        with self.assertRaises(ValueError):
            await recovered.approve(pending.approval_request, approved_by="owner")
        self.assertNotIn(b"private source", self.path.read_bytes())

    async def test_corrupt_and_unsupported_rows_are_ignored(self):
        engine = Orchestrator(store_path=str(self.path))
        task = Task.new("Read", "read", permitted_tools={"model.reply"})
        engine._remember(task)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("INSERT INTO tasks VALUES (?, ?)", ("a" * 32, "{broken"))
                db.execute("INSERT INTO tasks VALUES (?, ?)",
                           ("b" * 32, json.dumps({"schema": 999})))
        recovered = Orchestrator(store_path=str(self.path))
        self.assertEqual(recovered.get(task.task_id).status, Status.INTERRUPTED)
        self.assertIsNone(recovered.get("a" * 32))
        self.assertIsNone(recovered.get("b" * 32))

    async def test_corrupt_database_fails_closed_without_startup_crash(self):
        self.path.write_bytes(b"not a sqlite database")
        engine = Orchestrator(store_path=str(self.path))
        task = Task.new("Read", "read", permitted_tools={"model.reply"})
        result = await engine.run(task, "hello")
        self.assertEqual(result.task.status, Status.PENDING)
        self.assertIn("storage", result.error)

    async def test_expired_task_never_starts(self):
        engine = Orchestrator(store_path=str(self.path))
        calls = 0

        async def handler(_task, _payload):
            nonlocal calls
            calls += 1
            return "done"

        engine.register("read", "model.reply", handler,
                        valid_input=lambda _: True, valid_result=lambda _: True)
        task = Task.new("Read", "read", permitted_tools={"model.reply"},
                        timeout_seconds=0.01)
        with patch("task_engine._now", return_value=task.deadline_at + timedelta(seconds=1)):
            result = await engine.run(task)
        self.assertEqual((result.task.status, result.task.error_code),
                         (Status.FAILED, "timeout"))
        self.assertEqual(calls, 0)

    async def test_stale_approval_is_revoked(self):
        engine = Orchestrator(store_path=str(self.path))
        engine.register("write", "repository.stage", lambda *_: None,
                        valid_input=lambda value: isinstance(value, dict),
                        valid_result=lambda value: True)
        task = Task.new("Stage", "write", permitted_tools={"repository.stage"},
                        target="owner/repo:branch", timeout_seconds=1)
        pending = await engine.run(task, {})
        with patch("task_engine._now", return_value=task.deadline_at + timedelta(seconds=1)):
            self.assertEqual(engine.get(task.task_id).status, Status.FAILED)
            with self.assertRaises(ValueError):
                await engine.approve(pending.approval_request, approved_by="owner")
        self.assertEqual(engine.get(task.task_id).error_code, "timeout")

    async def test_external_handler_failure_is_uncertain_without_retry(self):
        engine = Orchestrator(store_path=str(self.path))
        calls = 0

        async def external(_task, _payload):
            nonlocal calls
            calls += 1
            raise RuntimeError("unknown outcome")

        engine.register("write", "repository.push", external,
                        valid_input=lambda value: isinstance(value, dict),
                        valid_result=lambda value: True)
        task = Task.new("Push", "write", permitted_tools={"repository.push"},
                        target="owner/repo:branch", max_attempts=3)
        pending = await engine.run(task, {})
        await engine.approve(pending.approval_request, approved_by="owner")
        outcome = await engine.run(task, {})
        self.assertEqual((outcome.task.status, outcome.task.error_code),
                         (Status.INTERRUPTED, "outcome_uncertain"))
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
