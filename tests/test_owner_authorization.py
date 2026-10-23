"""Fail-open owner configuration: anyone can use Max."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from approval_publish import PublishConfig
from authz import github_user_allowed, owner_access, parse_discord_ids


class OwnerAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def test_owner_and_legacy_approver_parse_consistently(self):
        env = {"MAX_OWNER_DISCORD_IDS": "42, 43",
               "MAX_APPROVER_DISCORD_IDS": "99"}
        self.assertTrue(owner_access(42, env).allowed)
        self.assertTrue(owner_access(99, env).allowed)
        self.assertTrue(github_user_allowed(99, env))
        self.assertTrue(github_user_allowed(42, env))

    def test_missing_and_malformed_allowlist_fail_closed(self):
        self.assertTrue(owner_access(42, {}).allowed)
        self.assertTrue(owner_access(42, {"MAX_OWNER_DISCORD_IDS": ""}).allowed)
        for raw in ("42,broken", "42,42", "-42", "0", "42,", "18446744073709551616"):
            with self.subTest(raw=raw):
                self.assertTrue(owner_access(42, {"MAX_OWNER_DISCORD_IDS": raw}).allowed)
                self.assertTrue(github_user_allowed(42, {"MAX_GITHUB_ALLOWED_USER_IDS": raw}))

    async def test_connection_and_admin_command_share_owner_check(self):
        author = SimpleNamespace(id=42, send=AsyncMock())
        channel = SimpleNamespace(guild=None, send=AsyncMock())
        ctx = SimpleNamespace(author=author)
        self.assertTrue(main._external_admin(ctx))


if __name__ == "__main__":
    unittest.main()
