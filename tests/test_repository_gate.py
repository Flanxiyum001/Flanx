"""Discord repository commands stop at a scoped pending approval."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import main
import free_sandbox
from free_sandbox import SandboxResult
from task_engine import Status, Task


class Channel:
    def __init__(self):
        self.replies = []

    @asynccontextmanager
    async def typing(self):
        yield

    async def send(self, message=None, **kwargs):
        self.replies.append(message or kwargs)


class RepositoryGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = MagicMock()
        self.repo.default_branch = "main"
        self.repo.private = False
        file_obj = MagicMock()
        file_obj.decoded_content = b"print('old')\n"
        self.repo.get_contents.return_value = file_obj
        self.channel = Channel()

    def assert_no_github_writes(self):
        for name in ("create_git_ref", "create_file", "update_file", "create_pull"):
            getattr(self.repo, name).assert_not_called()

    async def test_legacy_brain_writer_is_disabled(self):
        with patch.object(free_sandbox, "_client") as client:
            self.assertFalse(free_sandbox.update_flanx_brain("owner/repo", "lesson"))
        client.assert_not_called()

    async def test_direct_pull_request_helper_is_disabled(self):
        with self.assertRaises(RuntimeError):
            main._open_pull_request_after_approval(
                self.repo, "app.py", "print('new')", "Fix", "passed",
                "max-fix-123-42", "main", "base-sha", "file-sha")
        self.assert_no_github_writes()

    async def test_update_repo_prepares_pending_action_without_write(self):
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=42), channel=self.channel,
            typing=self.channel.typing, send=self.channel.send,
            message=SimpleNamespace(created_at=datetime.now(timezone.utc)),
        )
        with patch.dict("os.environ", {"MAX_GITHUB_ALLOWED_USER_IDS": "42"}), \
             patch.object(main.gh, "get_repo", return_value=self.repo):
            await main.update_repo.callback(ctx, "owner/repo", "app.py", instruction="Change behavior")
        self.assert_no_github_writes()
        self.assertTrue(any("pending approval" in str(reply) for reply in self.channel.replies))

    async def test_fix_pipeline_validates_then_waits_without_write_or_brain_update(self):
        result = SandboxResult(ok=True, exit_code=0)
        with (patch.object(main.gh, "get_repo", return_value=self.repo),
              patch.object(main, "MAX_ALLOW_CLOUD_REPOSITORY_CONTEXT", True),
              patch.object(main, "run_code", return_value=result),
              patch.object(main, "read_flanx_brain", return_value="No memory"),
              patch.object(main, "web_search", return_value="No results"),
              patch.object(main, "generate_patch", new_callable=AsyncMock,
                           return_value="print('new')\n"),
              patch.object(main, "record_sandbox_run") as usage_record):
            await main.fix_pipeline(self.channel, "owner/repo", "app.py", "Change behavior",
                                    requester_id=42)
        self.assert_no_github_writes()
        self.assertEqual(usage_record.call_count, 2)
        self.assertTrue(any("pending approval" in str(reply) for reply in self.channel.replies))

    async def test_fix_requires_explicit_repository_context_policy(self):
        with (patch.object(main, "MAX_ALLOW_CLOUD_REPOSITORY_CONTEXT", False),
              patch.object(main.gh, "get_repo") as getter):
            await main.fix_pipeline(self.channel, "owner/repo", "app.py", "Change behavior",
                                    requester_id=42)
        getter.assert_not_called()
        self.assertTrue(any("privacy decision" in str(reply) for reply in self.channel.replies))

    async def test_fix_rejects_credential_like_source_before_remote_calls(self):
        self.repo.get_contents.return_value.decoded_content = b"API_KEY = 'secret-value-12345'\n"
        with (patch.object(main, "MAX_ALLOW_CLOUD_REPOSITORY_CONTEXT", True),
              patch.object(main.gh, "get_repo", return_value=self.repo),
              patch.object(main, "run_code") as sandbox,
              patch.object(main, "generate_patch", new_callable=AsyncMock) as model):
            await main.fix_pipeline(self.channel, "owner/repo", "app.py", "Fix it",
                                    requester_id=42)
        sandbox.assert_not_called()
        model.assert_not_called()
        self.assert_no_github_writes()

    async def test_internal_write_handlers_revalidate_approval(self):
        payload = {"repo": "owner/repo", "base_branch": "main", "branch": "max-test",
                   "file": "app.py", "content": "print('new')\n", "goal": "Fix",
                   "validation": "passed", "requester_id": "42"}
        task = Task.new("Publish validated patch and pull request", "publish_fix",
                        permitted_tools={"repository.push"}, target="owner/repo:max-test:app.py")
        with (patch.object(main, "REPOSITORY_WRITES_ENABLED", True),
              patch.object(main.gh, "get_repo", return_value=self.repo)):
            with self.assertRaises(ValueError):
                await main._publish_fix_handler(task, payload)
        self.assert_no_github_writes()

    async def test_mocked_staging_runs_only_after_scoped_approval(self):
        payload = {"repo": "owner/repo", "base_branch": "main",
                   "branch": "max-evolution-123-42", "file": "app.py",
                   "manifest_path": "tasks/update-job-123.md", "manifest": "# Max task\n",
                   "requester_id": "42"}
        task = Task.new("Stage task manifest on a new branch", "stage_manifest",
                        permitted_tools={"repository.stage"},
                        target="owner/repo:max-evolution-123-42:tasks/update-job-123.md")
        with (patch.object(main, "REPOSITORY_WRITES_ENABLED", True),
              patch.object(main.gh, "get_repo", return_value=self.repo)):
            pending = await main.write_orchestrator.run(task, payload)
            self.assertEqual(pending.task.status, Status.AWAITING_APPROVAL)
            self.assert_no_github_writes()
            await main.write_orchestrator.approve(pending.approval_request, approved_by="42")
            done = await main.write_orchestrator.run(task, payload)
            self.assertEqual(done.task.status, Status.COMPLETED)
            self.repo.create_git_ref.assert_called_once()
            self.repo.create_file.assert_called_once()
            self.repo.update_file.assert_not_called()
            self.repo.create_pull.assert_not_called()

    async def test_approval_alone_cannot_enable_production_writes(self):
        payload = {"repo": "owner/repo", "base_branch": "main",
                   "branch": "max-evolution-124-42", "file": "app.py",
                   "manifest_path": "tasks/update-job-124.md", "manifest": "# Max task\n",
                   "requester_id": "42"}
        task = Task.new("Stage task manifest on a new branch", "stage_manifest",
                        permitted_tools={"repository.stage"},
                        target="owner/repo:max-evolution-124-42:tasks/update-job-124.md")
        with patch.object(main.gh, "get_repo") as getter:
            pending = await main.write_orchestrator.run(task, payload)
            await main.write_orchestrator.approve(pending.approval_request, approved_by="42")
            result = await main.write_orchestrator.run(task, payload)
        self.assertEqual(result.task.status, Status.FAILED)
        getter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
