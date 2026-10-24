"""GitHub visibility uses only authorized, live-discovered read actions."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from composio_adapter import Action, CapabilityPolicy, ComposioAdapter, IntegrationError
from external_actions import ExternalActionService
from github_visibility import IDENTITY_ACTION, REPOSITORY_ACTIONS, github_visibility


REPOS = REPOSITORY_ACTIONS[0]
EMPTY = {"type": "object", "properties": {}, "required": []}
PAGED = {"type": "object", "properties": {
    "page": {"type": "integer", "minimum": 1},
    "per_page": {"type": "integer", "minimum": 1, "maximum": 100}}, "required": []}


def policy():
    return CapabilityPolicy({"autonomy": "observe", "connections": {"github": "ac_github"},
        "github_repository_allowlist": ["real-org/alpha"], "actions": [
            {"slug": slug, "toolkit": "github", "account_id": "ca_github",
             "user_id": "max-discord-42", "version": "v1", "risk": "read",
             "category": "github", "max_calls": 3}
            for slug in (IDENTITY_ACTION, REPOS)]})


class FakeGitHubSDK:
    def __init__(self):
        self.connected_accounts = SimpleNamespace(list=self.list_accounts)
        self.tools = SimpleNamespace(get_raw_composio_tool_by_slug=self.tool,
                                     execute=self.execute)
        self.connected = True
        self.version = "v1"
        self.calls = []
        self.fail = False
        self.repos = [
            {"full_name": "real-org/alpha", "private": True,
             "permissions": {"pull": True, "push": False, "admin": False}},
            {"full_name": "real-org/beta", "private": False},
        ]

    def list_accounts(self, *, user_ids, statuses=None):
        if not self.connected:
            return {"items": []}
        return {"items": [{"id": "ca_github", "user_id": "max-discord-42",
                           "toolkit": {"slug": "github"}, "status": "ACTIVE"}]}

    def tool(self, slug):
        return {"slug": slug, "version": self.version,
                "input_parameters": EMPTY if slug == IDENTITY_ACTION else PAGED}

    def execute(self, slug, *, arguments, connected_account_id, user_id, version):
        self.calls.append((slug, arguments, connected_account_id, user_id, version))
        if self.fail:
            raise OSError("private upstream details")
        if slug == IDENTITY_ACTION:
            return {"successful": True, "data": {"login": "actual-login"}}
        return {"successful": True, "data": {"repositories": self.repos,
                                            "access_token": "must-not-be-shown"}}


class GitHubVisibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env_patch = patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "42",
                                                  "MAX_GITHUB_ALLOWED_USER_IDS": "42"})
        self.env_patch.start()
        self.sdk = FakeGitHubSDK()
        self.adapter = ComposioAdapter(policy(), client=self.sdk)
        root = Path(self.temp.name)
        self.service = ExternalActionService(self.adapter, store_path=str(root / "tasks.db"),
                                             audit_path=str(root / "audit.db"))

    def tearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    async def test_actual_identity_repositories_and_api_permissions(self):
        report = await github_visibility(self.adapter, self.service, 42)
        self.assertEqual(report["login"], "actual-login")
        self.assertEqual([item["name"] for item in report["visible"]],
                         ["real-org/alpha", "real-org/beta"])
        self.assertEqual(report["authorized"], ["real-org/alpha"])
        self.assertEqual(report["visible"][0]["permissions"],
                         {"admin": False, "push": False, "pull": True})
        self.assertNotIn("permissions", report["visible"][1])
        self.assertNotIn("must-not-be-shown", str(report))
        self.assertEqual([call[0] for call in self.sdk.calls], [IDENTITY_ACTION, REPOS])
        self.assertTrue(all(call[2:] == ("ca_github", "max-discord-42", "v1")
                            for call in self.sdk.calls))

    async def test_missing_connection_and_action_disabled(self):
        self.sdk.connected = False
        with self.assertRaises(IntegrationError) as caught:
            await github_visibility(self.adapter, self.service, 42)
        self.assertEqual(caught.exception.code, "github_connection_missing")
        self.sdk.connected = True
        self.adapter.policy.disabled_actions.add(REPOS)
        with self.assertRaises(IntegrationError) as caught:
            await github_visibility(self.adapter, self.service, 42)
        self.assertEqual(caught.exception.code, "permission_denied")
        self.assertEqual(self.sdk.calls, [])
        self.adapter.policy.disabled_actions.clear()
        del self.adapter.policy.actions[("ca_github", REPOS)]
        with self.assertRaises(IntegrationError) as caught:
            await github_visibility(self.adapter, self.service, 42)
        self.assertEqual(caught.exception.code, "github_read_action_missing")

    async def test_schema_mismatch_and_api_error(self):
        self.sdk.version = "v2"
        with self.assertRaises(IntegrationError) as caught:
            await github_visibility(self.adapter, self.service, 42)
        self.assertEqual(caught.exception.code, "schema_mismatch")
        self.assertEqual(self.sdk.calls, [])
        self.sdk.version = "v1"
        self.sdk.fail = True
        with self.assertRaises(IntegrationError) as caught:
            await github_visibility(self.adapter, self.service, 42)
        self.assertEqual(caught.exception.code, "service_error")
        self.assertNotIn("private upstream", str(caught.exception))

    async def test_ownership_schema_and_malformed_results_fail_closed(self):
        self.adapter.policy.actions[("ca_github", REPOS)] = self.adapter.policy.actions[("ca_github", REPOS)].__class__(
            REPOS, "github", "ca_github", "max-discord-99", "v1", "read", "github", 3)
        with self.assertRaises(IntegrationError) as caught:
            await github_visibility(self.adapter, self.service, 42)
        self.assertEqual(caught.exception.code, "permission_denied")
        self.adapter.policy = policy()
        self.sdk.tools.get_raw_composio_tool_by_slug = lambda slug: {
            "slug": slug, "version": "v1", "input_parameters": {
                "type": "object", "properties": {"owner": {"type": "string"}},
                "required": ["owner"]}}
        with self.assertRaises(IntegrationError) as caught:
            await github_visibility(self.adapter, self.service, 42)
        self.assertEqual(caught.exception.code, "invalid_arguments")
        self.sdk.tools.get_raw_composio_tool_by_slug = self.sdk.tool
        self.sdk.repos = [{"name": "fabricated"}]
        with self.assertRaises(IntegrationError) as caught:
            await github_visibility(self.adapter, self.service, 42)
        self.assertEqual(caught.exception.code, "invalid_response")

    async def test_repo_allowlist_is_enforced_for_other_github_actions(self):
        action = Action("GITHUB_GET_A_REPOSITORY", "github", "ca_github",
                        "max-discord-42", "v1", "read", "github", 2)
        self.adapter.policy.check_repository_scope(action,
            {"owner": "real-org", "repo": "alpha"})
        for arguments in ({"owner": "real-org", "repo": "beta"},
                          {"owner": "real-org"}, {}):
            with self.subTest(arguments=arguments), self.assertRaises(IntegrationError):
                self.adapter.policy.check_repository_scope(action, arguments)

    async def test_discord_request_gets_private_report_without_model(self):
        self.assertEqual(main.chat_intent("Which repositories do you have access to?")[0],
                         "github_visibility")
        author = SimpleNamespace(id=42, send=AsyncMock())
        channel = SimpleNamespace(guild=object(), send=AsyncMock())
        with patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "42"}), \
             patch.object(main, "github_user_allowed", return_value=True), \
             patch.object(main, "composio_adapter", self.adapter), \
             patch.object(main, "external_service", self.service), \
             patch.object(main.model_router, "generate", new_callable=AsyncMock) as model:
            await main._handle_github_visibility(author, channel)
        model.assert_not_called()
        text = author.send.await_args.args[0]
        self.assertIn("actual-login", text)
        self.assertIn("real-org/alpha", text)
        self.assertIn("real-org/beta", text)
        self.assertNotIn("must-not-be-shown", text)
        self.assertEqual(channel.send.await_args.args[0],
                         "GitHub visibility details were sent by DM.")


if __name__ == "__main__":
    unittest.main()
