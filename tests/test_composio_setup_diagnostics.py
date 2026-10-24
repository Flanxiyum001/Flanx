"""Fail-closed Composio setup diagnostics; never access live account secrets."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from composio_adapter import composio_setup_issues


class SetupDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.file = Path(self.temp.name) / "private-policy.json"
        self.ctx = SimpleNamespace(
            guild=None,
            author=SimpleNamespace(id=42),
            send=AsyncMock(),
        )
        self.base = {
            "MAX_OWNER_DISCORD_IDS": "42",
            "MAX_COMPOSIO_ENABLED": "true",
            "MAX_EXTERNAL_ACTIONS_DISABLED": "false",
            "COMPOSIO_API_KEY": "synthetic-private-example",
            "MAX_COMPOSIO_POLICY_FILE": str(self.file),
        }

    def tearDown(self):
        self.temp.cleanup()

    def write_policy(self, *, auth="ac_sample"):
        self.file.write_text(json.dumps({
            "autonomy": "observe", "connections": {"github": auth},
            "github_repository_allowlist": [], "actions": [],
        }), encoding="utf-8")

    def test_default_is_blocked_and_no_secrets_disclosed(self):
        issues = composio_setup_issues({})
        self.assertTrue(any("MAX_COMPOSIO_ENABLED" in item for item in issues))
        self.assertTrue(any("COMPOSIO_API_KEY" in item for item in issues))
        self.assertTrue(any("MAX_COMPOSIO_POLICY_FILE" in item for item in issues))
        self.assertTrue(composio_setup_issues({"COMPOSIO_API_KEY": "synthetic-private-value"}))
        self.assertNotIn("synthetic-private-value",
                         " ".join(composio_setup_issues(
                             {"COMPOSIO_API_KEY": "synthetic-private-value"})))

    def test_missing_file_and_invalid_json_detected(self):
        issues = composio_setup_issues(self.base)
        self.assertTrue(any("not found" in item for item in issues))
        self.file.write_text("{no-json", encoding="utf-8")
        issues = composio_setup_issues(self.base)
        self.assertTrue(any("invalid JSON" in item for item in issues))

    def test_invalid_policy_schema_is_detected(self):
        self.file.write_text('{"autonomy": "anything", "connections": {}}', encoding="utf-8")
        issues = composio_setup_issues(self.base)
        self.assertTrue(any("schema validation" in item for item in issues))

    def test_missing_github_auth_and_placeholder(self):
        self.file.write_text('{"autonomy": "observe", "connections": {}, "actions": []}', encoding="utf-8")
        self.assertTrue(any("lacks a GitHub" in x for x in composio_setup_issues(self.base)))
        self.write_policy(auth="replace-with-github-auth-config-id")
        self.assertTrue(any("placeholder" in x for x in composio_setup_issues(self.base)))

    def test_configured_but_not_live_verified(self):
        self.write_policy()
        self.assertEqual(composio_setup_issues(self.base), ())
        blocked = composio_setup_issues({
            **self.base, "MAX_EXTERNAL_ACTIONS_DISABLED": "true"})
        self.assertTrue(any("emergency" in issue for issue in blocked))
        self.assertNotIn(str(self.file), " ".join(blocked))

    async def test_owner_dm_command_gives_safe_actionable_errors(self):
        with patch.dict(os.environ, {"MAX_OWNER_DISCORD_IDS": "42",
                                      "MAX_COMPOSIO_ENABLED": "false",
                                      "COMPOSIO_API_KEY": "secret-never-print",
                                      "MAX_COMPOSIO_POLICY_FILE": str(self.file)}, clear=True), \\
             patch.object(main, "composio_adapter", None):
            await main.max_composio_status.callback(self.ctx)
        body = self.ctx.send.await_args.args[0]
        self.assertIn("Composio setup blockers", body)
        self.assertIn("MAX_COMPOSIO_ENABLED", body)
        self.assertIn("!max_connect github", body)
        self.assertNotIn("secret-never-print", body)
        self.assertNotIn(str(self.file), body)

    async def test_status_command_private_and_owner_only(self):
        with patch.dict(os.environ, {"MAX_OWNER_DISCORD_IDS": "99"}, clear=True):
            await main.max_composio_status.callback(self.ctx)
        self.assertIn("not in the configured owner allowlist",
                      self.ctx.send.await_args.args[0])
        self.ctx.send.reset_mock()
        self.ctx.guild = object()
        with patch.dict(os.environ, {"MAX_OWNER_DISCORD_IDS": "42"}, clear=True):
            await main.max_composio_status.callback(self.ctx)
        self.assertIn("in a DM", self.ctx.send.await_args.args[0])

    async def test_ready_message_does_not_claim_live_auth(self):
        self.write_policy()
        with patch.dict(os.environ, self.base, clear=True), \\
             patch.object(main, "composio_adapter", SimpleNamespace(
                 policy=SimpleNamespace(emergency_off=False))), \\
             patch.object(main, "composio_configuration_error", None):
            await main.max_composio_status.callback(self.ctx)
        self.assertIn("not live-verified", self.ctx.send.await_args.args[0])

    async def test_connect_error_points_to_status_command(self):
        author = SimpleNamespace(id=42, send=AsyncMock())
        channel = SimpleNamespace(guild=object(), send=AsyncMock())
        with patch.dict(os.environ, {"MAX_OWNER_DISCORD_IDS": "42"}, clear=True), \\
             patch.object(main, "composio_adapter", None):
            await main._handle_connection(
                author, channel, {"operation": "connect", "toolkit": "github"})
        self.assertIn("!max_composio_status", channel.send.await_args.args[0])
        author.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
