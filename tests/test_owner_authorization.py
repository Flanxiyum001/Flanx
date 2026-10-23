"""Fail-closed owner configuration: only configured IDs can use owner commands."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from approval_publish import PublishConfig
from authz import github_user_allowed, owner_access, parse_discord_ids


class OwnerAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def test_owner_and_legacy_approver_parse_consistently(self):
        env = {"MAX_OWNER_DISCORD_IDS": "42, 43",
               "MAX_APPROVER_DISCORD_IDS": "99",
               "MAX_GITHUB_ALLOWED_USER_IDS": "42"}
        self.assertTrue(owner_access(42, env).allowed)
        self.assertFalse(owner_access(99, env).allowed)
        self.assertFalse(github_user_allowed(99, env))
        self.assertTrue(github_user_allowed(42, env))

    def test_legacy_fallback_when_owner_variable_absent(self):
        env = {"MAX_APPROVER_DISCORD_IDS": "99"}
        self.assertTrue(owner_access(99, env).allowed)
        self.assertFalse(owner_access(42, env).allowed)

    def test_missing_owner_configuration_fails_closed(self):
        self.assertFalse(owner_access(42, {}).allowed)

    def test_empty_primary_owner_variable_fails_closed_no_fallback(self):
        self.assertFalse(owner_access(42, {"MAX_OWNER_DISCORD_IDS": ""}).allowed)

    def test_malformed_owner_allowlist_fails_closed(self):
        for raw in ("42,broken", "42,42", "-42", "0", "42,", "18446744073709551616", ""):
            with self.subTest(raw=raw):
                self.assertFalse(owner_access(42, {"MAX_OWNER_DISCORD_IDS": raw}).allowed)

    def test_github_user_allowed_requires_explicit_allowlist(self):
        self.assertFalse(github_user_allowed(1234, {}))
        self.assertFalse(github_user_allowed(1234, {"MAX_GITHUB_ALLOWED_USER_IDS": "invalid"}))
        self.assertFalse(github_user_allowed(-1, {"MAX_GITHUB_ALLOWED_USER_IDS": "-1"}))
        self.assertFalse(github_user_allowed(1234, {"MAX_GITHUB_ALLOWED_USER_IDS": "12345"}))
        self.assertTrue(github_user_allowed(1234, {"MAX_GITHUB_ALLOWED_USER_IDS": "9, 1234,5678"}))

    def test_owner_and_github_checks_are_separate(self):
        env = {"MAX_OWNER_DISCORD_IDS": "42",
               "MAX_GITHUB_ALLOWED_USER_IDS": "99"}
        self.assertTrue(owner_access(42, env).allowed)
        self.assertFalse(owner_access(99, env).allowed)
        self.assertTrue(github_user_allowed(99, env))
        self.assertFalse(github_user_allowed(42, env))

    async def test_connection_and_admin_command_share_owner_check(self):
        author = SimpleNamespace(id=42, send=AsyncMock())
        channel = SimpleNamespace(guild=None, send=AsyncMock())
        ctx = SimpleNamespace(author=author)
        with patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "42"}):
            self.assertTrue(main._external_admin(ctx))
        with patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "99"}):
            self.assertFalse(main._external_admin(ctx))


if __name__ == "__main__":
    unittest.main()
