"""Offline Discord approval and GitHub publication tests."""

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from free_sandbox import SandboxResult
from approval_publish import PublishConfig, PublishError, PublishingCoordinator, _ids
from task_engine import Status


class MissingBranch(Exception):
    status = 404


class GitHubRepo:
    full_name = "owner/test-repo"
    default_branch = "main"

    def __init__(self):
        self.branch_written = False
        self.file_written = False
        self.pull_written = False
        self.fail_at = None
        self.writes = []
        self.base_sha = "a" * 40
        self.file_sha = "b" * 40
        self.original = b"print('old')\n"
        self.proposed = b"print('new')\n"

    def get_branch(self, name):
        if name == "main":
            return SimpleNamespace(commit=SimpleNamespace(sha=self.base_sha))
        if name == "max-fix-test" and self.branch_written:
            return SimpleNamespace(commit=SimpleNamespace(sha="c" * 40))
        raise MissingBranch()

    def get_contents(self, path, ref):
        if ref == "main":
            return SimpleNamespace(decoded_content=self.original, sha=self.file_sha)
        if ref == "max-fix-test" and self.branch_written:
            return SimpleNamespace(decoded_content=self.proposed if self.file_written else self.original,
                                   sha="d" * 40)
        raise MissingBranch()

    def create_git_ref(self, *, ref, sha):
        self.writes.append("branch")
        if self.fail_at == "branch":
            raise TimeoutError("private provider detail")
        self.branch_written = True

    def update_file(self, *, path, message, content, sha, branch):
        self.writes.append("file")
        if self.fail_at == "file":
            raise TimeoutError("private provider detail")
        self.proposed = content.encode()
        self.file_written = True

    def create_pull(self, *, title, body, head, base):
        self.writes.append("pull")
        if self.fail_at == "pull_before":
            raise ValueError("request rejected")
        self.pull_written = True  # a timeout can occur after the remote write
        if self.fail_at == "pull":
            raise TimeoutError("private provider detail")
        return self._pull()

    def _pull(self):
        return SimpleNamespace(html_url="https://github.com/owner/test-repo/pull/1",
                               head=SimpleNamespace(ref="max-fix-test",
                                                    repo=SimpleNamespace(full_name="owner/test-repo")),
                               base=SimpleNamespace(ref="main"),
                               get_files=lambda: [SimpleNamespace(filename="app.py")])

    def get_pulls(self, *, state, head, base):
        return [self._pull()] if self.pull_written else []


class GitHub:
    def __init__(self, repo):
        self.repo = repo
        self.calls = 0

    def get_repo(self, name):
        self.calls += 1
        if name != self.repo.full_name:
            raise ValueError("wrong repository")
        return self.repo


class PublishingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = GitHubRepo()
        self.github = GitHub(self.repo)
        self.config = PublishConfig(True, "owner/test-repo", frozenset({42}),
                                    "private-token", str(Path(self.tmp.name) / "publish.sqlite3"))
        self.service = PublishingCoordinator(self.config, lambda: self.github)

    async def prepare(self):
        return await self.service.prepare(repo="owner/test-repo", base="main",
                                          branch="max-fix-test", file="app.py",
                                          original="print('old')\n", content="print('new')\n",
                                          goal="Change behavior", validation="passed",
                                          validation_passed=True, requester_id=42)

    async def test_authorized_approval_creates_branch_file_and_pr_once(self):
        prepared = await self.prepare()
        self.assertIn("owner/test-repo", prepared.summary)
        self.assertIn("max-fix-test", prepared.summary)
        self.assertIn("app.py", prepared.summary)
        self.assertIn("Static review: 0 high", prepared.summary)
        self.assertEqual(self.repo.writes, [])
        result = await self.service.decide(prepared.task_id, prepared.nonce, 42, approve=True)
        self.assertEqual(result, "completed")
        self.assertEqual(self.repo.writes, ["branch", "file", "pull"])
        self.assertEqual(self.service.store.load(prepared.task_id)["state"], "completed")
        self.assertEqual(await self.service.decide(prepared.task_id, prepared.nonce, 42, approve=True), "unavailable")
        self.assertEqual(self.repo.writes, ["branch", "file", "pull"])

    async def test_unauthorized_replay_and_mismatch_are_rejected(self):
        prepared = await self.prepare()
        self.assertEqual(await self.service.decide(prepared.task_id, prepared.nonce, 99, approve=True), "unauthorized")
        self.assertEqual(await self.service.decide(prepared.task_id, "wrong", 42, approve=True), "mismatched_request")
        self.assertEqual(await self.service.decide("0" * 32, prepared.nonce, 42, approve=True), "unavailable")
        self.assertEqual(self.repo.writes, [])
        self.service.config = replace(self.config, approver_ids=frozenset({42, 43}))
        self.assertEqual(await self.service.decide(prepared.task_id, prepared.nonce, 43, approve=True),
                         "mismatched_request")

    async def test_missing_invalid_allowlist_and_credentials_fail_closed(self):
        self.assertEqual(_ids(""), frozenset())
        self.assertEqual(_ids("42,bad"), frozenset())
        self.assertEqual(_ids("42,42"), frozenset())
        for ids, token in ((frozenset(), "token"), (frozenset({42}), "")):
            config = replace(self.config, approver_ids=ids, token=token)
            service = PublishingCoordinator(config, lambda: self.github)
            with self.assertRaises(PublishError):
                await service.prepare(repo="owner/test-repo", base="main",
                                      branch="max-fix-test", file="app.py", original="old",
                                      content="new", goal="fix", validation="passed",
                                      validation_passed=True, requester_id=42)
        self.assertNotIn("private-token", repr(self.config))
        self.assertEqual(self.repo.writes, [])
        self.assertFalse(PublishConfig.from_env({"MAX_PUBLISH_ENABLED": "true",
                                                 "MAX_PUBLISH_TEST_REPO": "owner/test-repo",
                                                 "MAX_APPROVER_DISCORD_IDS": "bad",
                                                 "MAX_GITHUB_PUBLISH_TOKEN": "token"}).ready)

    async def test_expiry_cancellation_rejection_and_duplicate(self):
        prepared = await self.prepare()
        task, payload, nonce = self.service._pending[prepared.task_id]
        self.service._pending[prepared.task_id] = (
            replace(task, deadline_at=task.created_at + timedelta(seconds=0.001)), payload, nonce)
        await asyncio.sleep(0.01)
        self.assertEqual(await self.service.decide(prepared.task_id, nonce, 42, approve=True), "expired")
        self.assertEqual(self.repo.writes, [])

        second = await self.prepare()
        await self.service.engine.cancel(second.task_id)
        self.assertEqual(await self.service.decide(second.task_id, second.nonce, 42, approve=True), "stale_request")
        third = await self.prepare()
        self.assertEqual(await self.service.decide(third.task_id, third.nonce, 42, approve=False), "rejected")
        self.assertEqual(await self.service.decide(third.task_id, third.nonce, 42, approve=True), "unavailable")
        self.assertEqual(self.repo.writes, [])

    async def test_digest_repo_branch_and_diff_changes_block_approval(self):
        for field, changed in (("payload_digest", "0" * 64), ("repo", "owner/other"),
                               ("branch", "max-fix-other")):
            prepared = await self.prepare()
            record = self.service.store.load(prepared.task_id)
            self.assertTrue(self.service.store.save({**record, field: changed}))
            self.assertEqual(await self.service.decide(prepared.task_id, prepared.nonce, 42, approve=True),
                             "mismatched_request")
        prepared = await self.prepare()
        task, payload, nonce = self.service._pending[prepared.task_id]
        self.service._pending[prepared.task_id] = (task, {**payload, "content": "changed diff"}, nonce)
        self.assertEqual(await self.service.decide(prepared.task_id, nonce, 42, approve=True),
                         "mismatched_request")
        self.assertEqual(self.repo.writes, [])

    async def test_changed_source_after_approval_stops_before_write(self):
        prepared = await self.prepare()
        original_approve = self.service.engine.approve

        async def approve_then_change(*args, **kwargs):
            task = await original_approve(*args, **kwargs)
            self.repo.file_sha = "e" * 40
            return task

        self.service.engine.approve = approve_then_change
        self.assertEqual(await self.service.decide(prepared.task_id, prepared.nonce, 42, approve=True), "failed")
        self.assertEqual(self.repo.writes, [])

    async def test_changed_proposed_diff_after_approval_requires_new_request(self):
        prepared = await self.prepare()
        original_approve = self.service.engine.approve

        async def approve_then_change(*args, **kwargs):
            task = await original_approve(*args, **kwargs)
            self.service._pending[prepared.task_id][1]["content"] = "changed diff"
            return task

        self.service.engine.approve = approve_then_change
        self.assertEqual(await self.service.decide(prepared.task_id, prepared.nonce, 42, approve=True), "failed")
        self.assertEqual(self.repo.writes, [])

    async def test_branch_and_pull_failures_are_uncertain_without_retry(self):
        for stage in ("branch", "pull_before", "pull"):
            self.repo.fail_at = stage
            prepared = await self.prepare()
            self.assertEqual(await self.service.decide(prepared.task_id, prepared.nonce, 42, approve=True),
                             "outcome_uncertain")
            writes = list(self.repo.writes)
            self.assertEqual(await self.service.decide(prepared.task_id, prepared.nonce, 42, approve=True),
                             "unavailable")
            self.assertEqual(self.repo.writes, writes)
            self.repo = GitHubRepo()
            self.github.repo = self.repo

    async def test_reconcile_success_after_pr_timeout_is_read_only(self):
        self.repo.fail_at = "pull"
        prepared = await self.prepare()
        self.assertEqual(await self.service.decide(prepared.task_id, prepared.nonce, 42, approve=True),
                         "outcome_uncertain")
        writes = list(self.repo.writes)
        self.assertEqual(await self.service.reconcile(prepared.task_id), "completed")
        self.assertEqual(self.repo.writes, writes)
        self.assertEqual(await self.service.decide(prepared.task_id, prepared.nonce, 42, approve=True), "unavailable")
        restarted = PublishingCoordinator(self.config, lambda: self.github)
        self.assertEqual(await restarted.reconcile(prepared.task_id), "not_uncertain")

    async def test_reconcile_partial_branch_does_not_continue_write(self):
        self.repo.fail_at = "file"
        prepared = await self.prepare()
        self.assertEqual(await self.service.decide(prepared.task_id, prepared.nonce, 42, approve=True),
                         "outcome_uncertain")
        writes = list(self.repo.writes)
        self.assertEqual(await self.service.reconcile(prepared.task_id), "reconciliation_required")
        self.assertEqual(self.repo.writes, writes)

    async def test_restart_invalidates_pending_button(self):
        prepared = await self.prepare()
        restarted = PublishingCoordinator(self.config, lambda: self.github)
        self.assertEqual(await restarted.decide(prepared.task_id, prepared.nonce, 42, approve=True),
                         "unavailable")
        self.assertEqual(self.repo.writes, [])

    async def test_discord_view_uses_interaction_user_and_disables_after_reject(self):
        prepared = await self.prepare()
        with patch.object(main, "publisher", self.service):
            view = main.PublishApprovalView(prepared)
            interaction = SimpleNamespace(user=SimpleNamespace(id=99),
                                          response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                                          followup=SimpleNamespace(send=AsyncMock()), message=None)
            await view._decision(interaction, True)
            interaction.response.defer.assert_not_called()
            self.assertEqual(self.repo.writes, [])
            interaction.user.id = 42
            await view._decision(interaction, False)
            self.assertTrue(all(item.disabled for item in view.children))
            self.assertEqual(self.repo.writes, [])

    async def test_fix_command_prepares_discord_view_without_write(self):
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())))
        result = SandboxResult(ok=True, exit_code=0)
        with (patch.object(main, "publisher", self.service),
              patch.object(main, "MAX_ALLOW_CLOUD_REPOSITORY_CONTEXT", True),
              patch.object(main.gh, "get_repo", return_value=self.repo),
              patch.object(main, "run_code", return_value=result),
              patch.object(main, "read_flanx_brain", return_value="No memory"),
              patch.object(main, "web_search", return_value="No results"),
              patch.object(main, "generate_patch", new_callable=AsyncMock,
                           return_value="print('new')\n"),
              patch.object(main, "record_sandbox_run")):
            await main.fix_pipeline(channel, "owner/test-repo", "app.py", "Change behavior",
                                    requester_id=42)
        requests = [call for call in channel.send.call_args_list if "view" in call.kwargs]
        self.assertEqual(len(requests), 1)
        self.assertIsInstance(requests[0].kwargs["view"], main.PublishApprovalView)
        self.assertEqual(self.repo.writes, [])

    async def test_missing_github_client_or_storage_prevents_preparation(self):
        self.service.github_factory = lambda: None
        with self.assertRaises(PublishError):
            await self.prepare()
        self.service.github_factory = lambda: self.github
        self.service.store.available = False
        with self.assertRaises(PublishError):
            await self.prepare()
        self.assertEqual(self.repo.writes, [])

    async def test_static_review_blocks_credential_in_diff(self):
        with self.assertRaises(PublishError) as caught:
            await self.service.prepare(repo="owner/test-repo", base="main",
                                       branch="max-fix-test", file="app.py",
                                       original="print('old')\n",
                                       content="API_KEY = 'private-value-12345'\n",
                                       goal="Change", validation="passed",
                                       validation_passed=True, requester_id=42)
        self.assertEqual(str(caught.exception), "static_review_blocked")
        self.assertEqual(self.repo.writes, [])

    async def test_failed_validation_cannot_prepare(self):
        with self.assertRaises(PublishError) as caught:
            await self.service.prepare(repo="owner/test-repo", base="main",
                                       branch="max-fix-test", file="app.py",
                                       original="print('old')\n", content="print('new')\n",
                                       goal="Change", validation="failed",
                                       validation_passed=False, requester_id=42)
        self.assertEqual(str(caught.exception), "validation_not_passed")
        self.assertEqual(self.github.calls, 0)

    async def test_other_repository_and_legacy_staging_never_publish(self):
        with self.assertRaises(PublishError):
            await self.service.prepare(repo="owner/production", base="main",
                                       branch="max-fix-test", file="app.py",
                                       original="print('old')\n", content="print('new')\n",
                                       goal="Change", validation="passed",
                                       validation_passed=True, requester_id=42)

        @asynccontextmanager
        async def typing():
            yield

        send = AsyncMock()
        ctx = SimpleNamespace(author=SimpleNamespace(id=42), typing=typing, send=send,
                              message=SimpleNamespace(created_at=datetime.now(timezone.utc)))
        with (patch.object(main, "publisher", self.service),
              patch.object(main.gh, "get_repo", return_value=self.repo)):
            await main.update_repo.callback(ctx, "owner/test-repo", "app.py", instruction="Change")
        self.assertEqual(self.repo.writes, [])
        self.assertTrue(any("pending approval" in str(call.args) for call in send.call_args_list))

    async def test_status_command_requires_discord_identity_and_only_reads(self):
        prepared = await self.prepare()
        self.repo.fail_at = "pull"
        self.assertEqual(await self.service.decide(prepared.task_id, prepared.nonce, 42, approve=True),
                         "outcome_uncertain")
        writes = list(self.repo.writes)
        send = AsyncMock()
        ctx = SimpleNamespace(author=SimpleNamespace(id=99), send=send)
        with patch.object(main, "publisher", self.service):
            await main.max_publish_status.callback(ctx, prepared.task_id)
            self.assertIn("not authorized", send.call_args.args[0])
            ctx.author.id = 42
            await main.max_publish_status.callback(ctx, prepared.task_id)
            self.assertIn("completed", send.call_args.args[0])
        self.assertEqual(self.repo.writes, writes)


if __name__ == "__main__":
    unittest.main()
