"""Security regression checks for owner-scoped, read-only Composio actions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from authz import owner_access, github_user_allowed
from composio_adapter import CapabilityPolicy, ComposioAdapter, IntegrationError, MockComposioClient
from external_actions import ExternalActionService


SCHEMA = {"type": "object",
          "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}},
          "required": ["owner", "repo"]}
ACTION = {"slug": "GITHUB_GET_A_REPOSITORY", "toolkit": "github",
          "account_id": "ca_test", "user_id": "max-discord-42",
          "version": "v1", "risk": "read", "category": "github", "max_calls": 2}


def policy(entry=None):
    return CapabilityPolicy({"autonomy": "observe",
        "github_repository_allowlist": ["safe-org/repo"],
        "actions": [ACTION if entry is None else entry]})


class ComposioOwnerSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.client = MockComposioClient(SCHEMA)
        self.adapter = ComposioAdapter(policy(), client=self.client)
        self.service = ExternalActionService(
            self.adapter, store_path=str(root / "tasks.db"),
            audit_path=str(root / "audit.db"))
        self.env = patch.dict("os.environ",
                              {"MAX_OWNER_DISCORD_IDS": "42",
                               "MAX_GITHUB_ALLOWED_USER_IDS": "42"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    async def test_authorized_read_only_in_allowlist(self):
        result = await self.service.start("ca_test", ACTION["slug"],
                                          {"owner": "safe-org", "repo": "repo"},
                                          requester_id="42")
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(self.client.calls), 1)

    async def test_owner_and_github_gate_both_required(self):
        with patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "99",
                                       "MAX_GITHUB_ALLOWED_USER_IDS": "42"}):
            with self.assertRaises(IntegrationError) as caught:
                await self.service.start("ca_test", ACTION["slug"],
                                         {"owner": "safe-org", "repo": "repo"},
                                         requester_id="42")
            self.assertEqual(caught.exception.code, "permission_denied")
        with patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "42",
                                       "MAX_GITHUB_ALLOWED_USER_IDS": "99"}):
            with self.assertRaises(IntegrationError) as caught:
                await self.service.start("ca_test", ACTION["slug"],
                                         {"owner": "safe-org", "repo": "repo"},
                                         requester_id="42")
            self.assertEqual(caught.exception.code, "permission_denied")
        self.assertEqual(self.client.calls, [])

    async def test_legacy_fallback_only_when_owner_env_absent(self):
        with patch.dict("os.environ", {"MAX_APPROVER_DISCORD_IDS": "42",
                                       "MAX_GITHUB_ALLOWED_USER_IDS": "42"}, clear=True):
            self.assertTrue(owner_access(42).allowed)
        for raw in ("", "42,nope"):
            with self.subTest(raw=raw), patch.dict(
                "os.environ", {"MAX_OWNER_DISCORD_IDS": raw,
                               "MAX_APPROVER_DISCORD_IDS": "42",
                               "MAX_GITHUB_ALLOWED_USER_IDS": "42"}, clear=True):
                self.assertFalse(owner_access(42).allowed)
                with self.assertRaises(IntegrationError):
                    await self.service.start("ca_test", ACTION["slug"],
                                             {"owner": "safe-org", "repo": "repo"},
                                             requester_id="42")
        self.assertEqual(self.client.calls, [])

    async def test_cross_user_account_rejected_before_discovery(self):
        with patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "42,99",
                                       "MAX_GITHUB_ALLOWED_USER_IDS": "42,99"}):
            with self.assertRaises(IntegrationError) as caught:
                await self.service.start("ca_test", ACTION["slug"],
                                         {"owner": "safe-org", "repo": "repo"},
                                         requester_id="99")
            self.assertEqual(caught.exception.code, "ownership_mismatch")
        self.assertEqual(self.client.calls, [])

    async def test_invalid_unknown_and_extra_arguments_never_execute(self):
        for slug, args in (
            ("GITHUB_UNKNOWN", {"owner": "safe-org", "repo": "repo"}),
            (ACTION["slug"], {"owner": "safe-org", "repo": "other"}),
            (ACTION["slug"], {"owner": "safe-org", "repo": "repo", "unknown": 1}),
            (ACTION["slug"], {"owner": "safe-org"}),
        ):
            with self.subTest(slug=slug, args=args), self.assertRaises(IntegrationError):
                await self.service.start("ca_test", slug, args, requester_id="42")
        self.assertEqual(self.client.calls, [])

    async def test_mismatched_version_fails_closed(self):
        self.adapter.policy.actions[("ca_test", ACTION["slug"])] = (
            self.adapter.policy.actions[("ca_test", ACTION["slug"])].__class__(
                ACTION["slug"], "github", "ca_test", "max-discord-42",
                "different-version", "read", "github", 2))
        with self.assertRaises(IntegrationError) as caught:
            await self.service.start("ca_test", ACTION["slug"],
                                     {"owner": "safe-org", "repo": "repo"},
                                     requester_id="42")
        self.assertEqual(caught.exception.code, "schema_mismatch")
        self.assertEqual(self.client.calls, [])

    async def test_connection_list_cannot_substitute_another_owner(self):
        class WrongAccountClient(MockComposioClient):
            def list(self, *, user_ids, statuses):
                return {"items": [{"id": "ca_test", "user_id": "max-discord-99",
                                   "status": "ACTIVE"}]}
        self.adapter._client = WrongAccountClient(SCHEMA)
        with self.assertRaises(IntegrationError) as caught:
            await self.service.start("ca_test", ACTION["slug"],
                                     {"owner": "safe-org", "repo": "repo"},
                                     requester_id="42")
        self.assertEqual(caught.exception.code, "connection_expired")

    async def test_git_write_policy_and_emergency_disable(self):
        with self.assertRaises(ValueError):
            policy({**ACTION, "slug": "GITHUB_CREATE_ISSUE", "risk": "write"})
        self.adapter.policy.emergency_off = True
        with self.assertRaises(IntegrationError):
            await self.service.start("ca_test", ACTION["slug"],
                                     {"owner": "safe-org", "repo": "repo"},
                                     requester_id="42")
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
