"""Offline tests for task state, capability limits, approvals, and execution."""

import asyncio
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from task_engine import (ApprovalRequest, ApprovalStatus, Orchestrator, Permission,
                         Status, TOOLS, Task)


class TaskStateTests(unittest.TestCase):
    def test_creation_and_transitions(self):
        task = Task.new("Summarize a question", "summary", permitted_tools={"model.reply"},
                        timeout_seconds=30, max_attempts=2)
        self.assertEqual(task.status, Status.PENDING)
        self.assertEqual(task.attempt, 0)
        self.assertGreater(task.deadline_at, task.created_at)
        running = task.transition(Status.RUNNING, attempt=1)
        done = running.transition(Status.COMPLETED, result_summary="Result available")
        self.assertEqual(done.status, Status.COMPLETED)
        self.assertGreaterEqual(done.updated_at, running.updated_at)

    def test_invalid_transitions_and_limits(self):
        task = Task.new("Summarize a question", "summary", permitted_tools={"model.reply"})
        with self.assertRaises(ValueError):
            task.transition(Status.COMPLETED)
        with self.assertRaises(ValueError):
            task.transition(Status.RUNNING).transition(Status.COMPLETED).transition(Status.RUNNING)
        with self.assertRaises(ValueError):
            Task.new("short", "summary", permitted_tools={"model.reply"}, max_attempts=4)
        with self.assertRaises(ValueError):
            Task.new("api_key=private-value", "summary", permitted_tools={"model.reply"})

    def test_mutating_capabilities_require_approval(self):
        for tool in TOOLS.values():
            if tool.permission in (Permission.WRITE, Permission.DESTRUCTIVE,
                                   Permission.MESSAGE, Permission.COMMERCE, Permission.DEPLOY):
                with self.subTest(tool=tool.name):
                    self.assertTrue(tool.approval_required)
                    self.assertTrue(tool.target_required)


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = Orchestrator()
        self.calls = 0

        async def read_handler(task, payload):
            self.calls += 1
            return payload.upper()

        self.engine.register("reply", "model.reply", read_handler,
                             valid_input=lambda value: isinstance(value, str) and bool(value),
                             valid_result=lambda value: isinstance(value, str) and bool(value))

    def task(self, **kwargs):
        return Task.new("Reply to a question", "reply", permitted_tools={"model.reply"}, **kwargs)

    async def test_success_and_duplicate_completion(self):
        task = self.task()
        result = await self.engine.run(task, "hello")
        self.assertEqual(result.task.status, Status.COMPLETED)
        self.assertEqual(result.result, "HELLO")
        self.assertEqual(result.task.attempt, 1)
        duplicate = await self.engine.run(task, "hello")
        self.assertEqual(duplicate.task.status, Status.COMPLETED)
        self.assertIsNone(duplicate.result)
        self.assertEqual(self.calls, 1)

    async def test_unsupported_type_and_tool_allowlist(self):
        unknown = Task.new("Unknown request", "unknown", permitted_tools={"model.reply"})
        outcome = await self.engine.run(unknown, "hello")
        self.assertEqual(outcome.task.error_code, "unsupported_task")
        denied = Task.new("Reply to a question", "reply", permitted_tools={"repository.inspect"})
        outcome = await self.engine.run(denied, "hello")
        self.assertEqual(outcome.task.error_code, "permission_denied")
        unknown_tool = Task.new("Reply to a question", "reply", permitted_tools={"invented.tool"})
        outcome = await self.engine.run(unknown_tool, "hello")
        self.assertEqual(outcome.task.error_code, "permission_denied")
        role = self.task(requested_role="coder")
        outcome = await self.engine.run(role, "hello")
        self.assertEqual(outcome.task.error_code, "permission_denied")
        self.assertEqual(self.calls, 0)

    async def test_input_and_result_validation(self):
        invalid = await self.engine.run(self.task(), "")
        self.assertEqual(invalid.task.error_code, "invalid_input")
        unsupported_payload = await self.engine.run(self.task(), object())
        self.assertEqual(unsupported_payload.task.error_code, "invalid_input")
        self.assertEqual(self.calls, 0)

        async def empty_result(task, payload):
            return ""

        self.engine.register("empty", "model.reply", empty_result,
                             valid_input=lambda value: value is None,
                             valid_result=lambda value: isinstance(value, str) and bool(value))
        task = Task.new("Request a reply", "empty", permitted_tools={"model.reply"})
        failed = await self.engine.run(task)
        self.assertEqual(failed.task.error_code, "handler_error")
        self.assertIsNone(failed.result)

    async def test_approval_is_scoped_to_action_target_and_payload(self):
        calls = []

        async def push(task, payload):
            calls.append((task.target, payload))
            return "mocked branch created"

        self.engine.register("push", "repository.push", push,
                             valid_input=lambda value: isinstance(value, dict),
                             valid_result=lambda value: isinstance(value, str))
        task = Task.new("Push patch branch", "push", permitted_tools={"repository.push"},
                        target="owner/repo:feature")
        waiting = await self.engine.run(task, {"commit": "abc"})
        self.assertEqual(waiting.task.status, Status.AWAITING_APPROVAL)
        self.assertEqual(waiting.task.approval_status, ApprovalStatus.PENDING)
        self.assertEqual(calls, [])
        request = waiting.approval_request
        self.assertEqual((request.tool, request.target), ("repository.push", "owner/repo:feature"))
        with self.assertRaises(ValueError):
            await self.engine.approve(replace(request, target="owner/other:feature"), approved_by="user-1")
        approved = await self.engine.approve(request, approved_by="user-1")
        self.assertEqual(approved.approval_status, ApprovalStatus.APPROVED)
        changed = await self.engine.run(task, {"commit": "different"})
        self.assertEqual(changed.task.error_code, "approval_scope_mismatch")
        self.assertEqual(calls, [])

        other = Task.new("Push patch branch", "push", permitted_tools={"repository.push"},
                         target="owner/repo:feature")
        pending = await self.engine.run(other, {"commit": "abc"})
        self.assertEqual(pending.task.status, Status.AWAITING_APPROVAL)
        self.assertEqual(calls, [])
        await self.engine.approve(pending.approval_request, approved_by="user-1")
        result = await self.engine.run(other, {"commit": "abc"})
        self.assertEqual(result.task.status, Status.COMPLETED)
        self.assertEqual(calls, [("owner/repo:feature", {"commit": "abc"})])

    async def test_approval_identity_expiry_replay_and_consumption(self):
        calls = []

        async def write(task, payload):
            calls.append(payload)
            return "done"

        self.engine.register("scoped_write", "repository.stage", write,
                             valid_input=lambda value: isinstance(value, dict),
                             valid_result=lambda value: value == "done")
        task = Task.new("Stage manifest", "scoped_write",
                        permitted_tools={"repository.stage"}, target="owner/repo:branch:tasks/job.md")
        payload = {"requester_id": "discord-42", "manifest": "one"}
        waiting = await self.engine.run(task, payload)
        request = waiting.approval_request
        with self.assertRaises(ValueError):
            await self.engine.approve(request, approved_by="discord-99")
        with self.assertRaises(ValueError):
            await self.engine.approve(replace(request, target="owner/other:branch:tasks/job.md"),
                                      approved_by="discord-42")
        with self.assertRaises(ValueError):
            await self.engine.approve(replace(request, payload_digest="0" * 64),
                                      approved_by="discord-42")
        with patch("task_engine._now", return_value=request.expires_at + timedelta(seconds=1)):
            with self.assertRaises(ValueError):
                await self.engine.approve(request, approved_by="discord-42")
        self.assertEqual(calls, [])
        self.assertEqual(self.engine.get(task.task_id).status, Status.FAILED)
        task = Task.new("Stage manifest", "scoped_write",
                        permitted_tools={"repository.stage"}, target="owner/repo:branch:tasks/job.md")
        request = (await self.engine.run(task, payload)).approval_request
        await self.engine.approve(request, approved_by="discord-42")
        with self.assertRaises(ValueError):
            await self.engine.approve(request, approved_by="discord-42")
        mismatched = await self.engine.run(task, {"requester_id": "discord-42", "manifest": "two"})
        self.assertEqual(mismatched.task.error_code, "approval_scope_mismatch")
        self.assertEqual(calls, [])

        next_task = Task.new("Stage manifest", "scoped_write",
                             permitted_tools={"repository.stage"}, target="owner/repo:branch:tasks/job.md")
        pending = await self.engine.run(next_task, payload)
        await self.engine.approve(pending.approval_request, approved_by="discord-42")
        outcome = await self.engine.run(next_task, payload)
        self.assertEqual(outcome.task.status, Status.COMPLETED)
        await self.engine.run(next_task, payload)
        self.assertEqual(calls, [payload])

    async def test_denial_and_missing_target(self):
        async def write(task, payload):
            self.calls += 1
            return "done"

        self.engine.register("write", "user_data.delete", write,
                             valid_input=lambda value: value is None,
                             valid_result=lambda value: value == "done")
        missing = Task.new("Delete saved data", "write", permitted_tools={"user_data.delete"})
        outcome = await self.engine.run(missing)
        self.assertEqual(outcome.task.error_code, "missing_target")
        task = Task.new("Delete saved data", "write", permitted_tools={"user_data.delete"},
                        target="user:123")
        waiting = await self.engine.run(task)
        denied = await self.engine.deny(waiting.approval_request)
        self.assertEqual(denied.approval_status, ApprovalStatus.DENIED)
        self.assertEqual(self.calls, 0)

    async def test_timeout(self):
        blocker = asyncio.Event()

        async def blocked(task, payload):
            await blocker.wait()
            return "late"

        self.engine.register("blocked", "repository.inspect", blocked,
                             valid_input=lambda value: value is None,
                             valid_result=lambda value: isinstance(value, str))
        task = Task.new("Inspect a repository", "blocked", permitted_tools={"repository.inspect"},
                        timeout_seconds=0.02)
        outcome = await self.engine.run(task)
        self.assertEqual(outcome.task.status, Status.FAILED)
        self.assertEqual(outcome.task.error_code, "timeout")
        self.assertEqual(outcome.task.attempt, 1)

    async def test_bounded_retries_and_sanitized_failure(self):
        async def flaky(task, payload):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("api_key=do-not-print")
            return "done"

        self.engine.register("flaky", "model.reply", flaky,
                             valid_input=lambda value: value is None,
                             valid_result=lambda value: value == "done")
        task = Task.new("Retry a reply", "flaky", permitted_tools={"model.reply"}, max_attempts=3)
        outcome = await self.engine.run(task)
        self.assertEqual((outcome.task.status, outcome.task.attempt), (Status.COMPLETED, 3))
        self.assertEqual(self.calls, 3)

        self.calls = 0
        limited = Task.new("Retry a reply", "flaky", permitted_tools={"model.reply"}, max_attempts=2)
        failed = await self.engine.run(limited)
        self.assertEqual((failed.task.status, failed.task.attempt), (Status.FAILED, 2))
        self.assertEqual(failed.task.error_code, "handler_error")
        self.assertNotIn("do-not-print", repr(failed))

    async def test_cancellation_and_concurrent_duplicate(self):
        started = asyncio.Event()
        blocker = asyncio.Event()

        async def blocked(task, payload):
            self.calls += 1
            started.set()
            await blocker.wait()
            return "done"

        self.engine.register("hold", "repository.inspect", blocked,
                             valid_input=lambda value: value is None,
                             valid_result=lambda value: isinstance(value, str))
        task = Task.new("Inspect a repository", "hold", permitted_tools={"repository.inspect"})
        runner = asyncio.create_task(self.engine.run(task))
        await started.wait()
        duplicate = await self.engine.run(task)
        self.assertEqual(duplicate.task.status, Status.RUNNING)
        self.assertEqual(self.calls, 1)
        cancelled = await self.engine.cancel(task.task_id)
        self.assertEqual(cancelled.status, Status.CANCELLED)
        final = await runner
        self.assertEqual(final.task.status, Status.CANCELLED)

    async def test_same_id_with_different_request_is_rejected(self):
        task = self.task()
        await self.engine.run(task, "hello")
        with self.assertRaises(ValueError):
            await self.engine.run(replace(task, description="Different request"), "hello")

    async def test_completed_history_is_bounded(self):
        first = self.task()
        await self.engine.run(first, "hello")
        for _ in range(256):
            await self.engine.run(self.task(), "hello")
        self.assertIsNone(self.engine.get(first.task_id))
        self.assertEqual(len(self.engine._tasks), 256)


if __name__ == "__main__":
    unittest.main()
