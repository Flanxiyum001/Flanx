"""Fail-closed Composio setup diagnostics; never access live account secrets."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from composio_adapter import ComposioAdapter, IntegrationError, composio_setup_issues


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
        self.assertTrue(any("MAX_COMPOSIO_GITHUB_AUTH_CONFIG_ID" in item for item in issues))
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
                                      "MAX_COMPOSIO_POLICY_FILE": str(self.file)}, clear=True), \
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
        with patch.dict(os.environ, self.base, clear=True), \
             patch.object(main, "composio_adapter", SimpleNamespace(
                 policy=SimpleNamespace(emergency_off=False))), \
             patch.object(main, "composio_configuration_error", None):
            await main.max_composio_status.callback(self.ctx)
        self.assertIn("not live-verified", self.ctx.send.await_args.args[0])

    async def test_connect_error_points_to_status_command(self):
        author = SimpleNamespace(id=42, send=AsyncMock())
        channel = SimpleNamespace(guild=object(), send=AsyncMock())
        with patch.dict(os.environ, {"MAX_OWNER_DISCORD_IDS": "42"}, clear=True), \
             patch.object(main, "composio_adapter", None):
            await main._handle_connection(
                author, channel, {"operation": "connect", "toolkit": "github"})
        self.assertIn("!max_composio_status", channel.send.await_args.args[0])
        author.send.assert_not_awaited()


    def test_explicit_env_mode_needs_no_policy_file_and_enables_zero_actions(self):
        env = {**self.base, "MAX_COMPOSIO_GITHUB_AUTH_CONFIG_ID": "ac_synthetic_123"}
        self.assertFalse(self.file.exists())
        self.assertEqual(composio_setup_issues(env), ())
        adapter = ComposioAdapter.from_env(env)
        self.assertFalse(adapter.policy.emergency_off)
        self.assertEqual(adapter.policy.autonomy, "observe")
        self.assertEqual(adapter.policy.connections, {"github": "ac_synthetic_123"})
        self.assertEqual(adapter.policy.actions, {})
        self.assertEqual(adapter.policy.github_repository_allowlist, frozenset())
        with self.assertRaises(IntegrationError) as caught:
            adapter.policy.decide("ca_any", "GITHUB_CREATE_ISSUE")
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_env_fallback_rejects_invalid_id_and_placeholder(self):
        for value in ("replace-with-github-auth-config-id", "has spaces", "<example-id>",
                      "bad/url", "x", "https://composio.dev"):
            with self.subTest(value=value):
                env = {**self.base, "MAX_COMPOSIO_GITHUB_AUTH_CONFIG_ID": value}
                issues = composio_setup_issues(env)
                self.assertTrue(any("invalid or a placeholder" in issue for issue in issues))
                self.assertNotIn(value, " ".join(issues))
                with self.assertRaises(ValueError):
                    ComposioAdapter.from_env(env)

    def test_existing_valid_policy_has_priority_over_env_fallback(self):
        self.write_policy(auth="ac_from_file")
        env = {**self.base, "MAX_COMPOSIO_GITHUB_AUTH_CONFIG_ID": "ac_from_env"}
        self.assertEqual(composio_setup_issues(env), ())
        adapter = ComposioAdapter.from_env(env)
        self.assertEqual(adapter.policy.connections["github"], "ac_from_file")

    def test_invalid_existing_policy_must_not_be_ignored_by_env_mode(self):
        self.file.write_text("{bad json", encoding="utf-8")
        env = {**self.base, "MAX_COMPOSIO_GITHUB_AUTH_CONFIG_ID": "ac_from_env"}
        self.assertTrue(any("invalid JSON" in issue for issue in composio_setup_issues(env)))
        with self.assertRaises(json.JSONDecodeError):
            ComposioAdapter.from_env(env)
        self.file.write_text('{"autonomy":"invalid","actions":[]}', encoding="utf-8")
        self.assertTrue(any("schema validation" in issue for issue in composio_setup_issues(env)))
        with self.assertRaises(ValueError):
            ComposioAdapter.from_env(env)

    def test_emergency_switch_and_api_key_still_fail_closed(self):
        env = {**self.base, "MAX_COMPOSIO_GITHUB_AUTH_CONFIG_ID": "ac_synthetic_123"}
        disabled = ComposioAdapter.from_env({**env, "MAX_EXTERNAL_ACTIONS_DISABLED": "true"})
        self.assertTrue(disabled.policy.emergency_off)
        with self.assertRaises(IntegrationError) as caught:
            disabled._sdk()
        self.assertEqual(caught.exception.code, "disabled")
        provider_off = ComposioAdapter.from_env({**env, "MAX_COMPOSIO_ENABLED": "false"})
        self.assertTrue(provider_off.policy.emergency_off)
        with self.assertRaises(IntegrationError) as caught:
            ComposioAdapter.from_env({**env, "COMPOSIO_API_KEY": ""})
        self.assertEqual(caught.exception.code, "configuration")

    async def test_connection_only_mode_can_produce_mock_hosted_link(self):
        class FakeAccounts:
            def __init__(self):
                self.link_calls = []

            def list(self, *, user_ids):
                return {"items": []}

            def link(self, user_id, auth_id):
                self.link_calls.append((user_id, auth_id))
                return {"id": "ca_synthetic_42",
                        "redirect_url": "https://connect.composio.dev/synthetic-opaque-url"}

        accounts = FakeAccounts()
        sdk = SimpleNamespace(
            connected_accounts=accounts,
            auth_configs=SimpleNamespace(
                get=lambda config_id: {"toolkit": {"slug": "github"}, "scopes": ["read"]}),
        )
        env = {**self.base, "MAX_COMPOSIO_GITHUB_AUTH_CONFIG_ID": "ac_synthetic_123"}
        adapter = ComposioAdapter.from_env(env)
        adapter._client = sdk
        with patch.dict(os.environ, env, clear=True), \
             patch.object(main, "composio_adapter", adapter):
            author = SimpleNamespace(id=42, send=AsyncMock())
            channel = SimpleNamespace(guild=object(), send=AsyncMock())
            await main._handle_connection(
                author, channel, {"operation": "connect", "toolkit": "github"})
        self.assertEqual(accounts.link_calls, [("max-discord-42", "ac_synthetic_123")])
        self.assertIn("connect.composio.dev", author.send.await_args.args[0])
        self.assertEqual(adapter.policy.actions, {})

    async def test_env_bootstrap_diagnostics_report_ready_without_path_leak(self):
        env = {**self.base, "MAX_COMPOSIO_GITHUB_AUTH_CONFIG_ID": "ac_synthetic_123"}
        adapter = ComposioAdapter.from_env(env)
        with patch.dict(os.environ, env, clear=True), \
             patch.object(main, "composio_adapter", adapter), \
             patch.object(main, "composio_configuration_error", None):
            await main.max_composio_status.callback(self.ctx)
        output = self.ctx.send.await_args.args[0]
        self.assertIn("not live-verified", output)
        self.assertNotIn("ac_synthetic_123", output)
        self.assertNotIn(str(self.file), output)


if __name__ == "__main__":
    unittest.main()
