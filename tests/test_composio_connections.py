"""Connect Link lifecycle, ownership, and Discord privacy with a fake SDK."""

import io
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from composio_adapter import CapabilityPolicy, ComposioAdapter, IntegrationError


def connection_policy():
    return CapabilityPolicy({"autonomy": "observe", "connections": {
        "github": "ac_github", "googlecalendar": "ac_calendar",
    }, "actions": []})


class FakeAccounts:
    def __init__(self):
        self.accounts = {}
        self.links = []
        self.deleted = []
        self.status = "INITIALIZING"

    def list(self, *, user_ids):
        return {"items": [item for item in self.accounts.values()
                          if item["user_id"] in user_ids]}

    def link(self, user_id, auth_config_id):
        self.links.append((user_id, auth_config_id))
        name = "github" if auth_config_id == "ac_github" else "googlecalendar"
        account_id = "ca_" + name
        self.accounts[account_id] = {"id": account_id, "user_id": user_id,
                                     "toolkit": {"slug": name}, "status": self.status}
        return {"id": account_id, "redirect_url":
                "https://connect.composio.dev/private-token-value"}

    def get(self, account_id):
        return self.accounts[account_id]

    def delete(self, account_id):
        self.deleted.append(account_id)
        del self.accounts[account_id]


class FakeSDK:
    def __init__(self):
        self.connected_accounts = FakeAccounts()
        self.auth_configs = SimpleNamespace(get=self.auth_config)

    def auth_config(self, auth_id):
        return {"toolkit": {"slug": "github" if auth_id == "ac_github" else "googlecalendar"},
                "scopes": ["repo:read"]}


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sdk = FakeSDK()
        self.adapter = ComposioAdapter(connection_policy(), client=self.sdk)

    async def test_link_status_and_no_action_enablement(self):
        link = await self.adapter.initiate_connection("Google Calendar", 42)
        self.assertEqual(self.sdk.connected_accounts.links,
                         [("max-discord-42", "ac_calendar")])
        self.assertIn("connect.composio.dev", link["url"])
        self.assertEqual(link["scopes"], ["repo:read"])
        pending = await self.adapter.connection_status(link["id"], 42)
        self.assertEqual(pending["status"], "INITIALIZING")
        self.assertEqual(pending["scopes"], ["repo:read"])
        self.assertFalse(pending["actions_enabled"])
        self.sdk.connected_accounts.accounts[link["id"]]["status"] = "ACTIVE"
        active = await self.adapter.connection_status(link["id"], 42)
        self.assertEqual(active["status"], "ACTIVE")
        self.assertEqual(active["action_categories"], [])

    async def test_duplicate_pending_active_and_expired(self):
        link = await self.adapter.initiate_connection("github", 42)
        with self.assertRaises(IntegrationError) as caught:
            await self.adapter.initiate_connection("github", 42)
        self.assertEqual(caught.exception.code, "connection_pending")
        account = self.sdk.connected_accounts.accounts[link["id"]]
        account["status"] = "ACTIVE"
        with self.assertRaises(IntegrationError) as caught:
            await self.adapter.initiate_connection("github", 42)
        self.assertEqual(caught.exception.code, "already_connected")
        account["status"] = "EXPIRED"
        self.assertEqual((await self.adapter.connection_status(link["id"], 42))["status"], "EXPIRED")
        await self.adapter.initiate_connection("github", 42)

    async def test_ownership_and_disconnect(self):
        link = await self.adapter.initiate_connection("github", 42)
        with self.assertRaises(IntegrationError) as caught:
            await self.adapter.connection_status(link["id"], 99)
        self.assertEqual(caught.exception.code, "ownership_mismatch")
        with self.assertRaises(IntegrationError):
            await self.adapter.disconnect_connection(link["id"], 99)
        self.assertEqual(self.sdk.connected_accounts.deleted, [])
        removed = await self.adapter.disconnect_connection(link["id"], 42)
        self.assertEqual(removed["status"], "DELETED")
        self.assertEqual(self.sdk.connected_accounts.deleted, [link["id"]])

    async def test_unconfigured_malformed_disabled_and_bad_link(self):
        for name, code in (("github/../", "invalid_toolkit"),
                           ("slack", "auth_config_missing")):
            with self.subTest(name=name), self.assertRaises(IntegrationError) as caught:
                await self.adapter.initiate_connection(name, 42)
            self.assertEqual(caught.exception.code, code)
        self.adapter.policy.disabled_integrations.add("github")
        with self.assertRaises(IntegrationError) as caught:
            await self.adapter.initiate_connection("github", 42)
        self.assertEqual(caught.exception.code, "toolkit_disabled")
        self.adapter.policy.disabled_integrations.clear()
        self.sdk.auth_configs.get = lambda *_: {"toolkit": {"slug": "slack"}}
        with self.assertRaises(IntegrationError) as caught:
            await self.adapter.initiate_connection("github", 42)
        self.assertEqual(caught.exception.code, "auth_config_mismatch")
        self.sdk.auth_configs.get = self.sdk.auth_config
        self.sdk.connected_accounts.link = lambda *_: {
            "id": "ca_bad", "redirect_url": "http://example.com/not-private"}
        with self.assertRaises(IntegrationError) as caught:
            await self.adapter.initiate_connection("github", 42)
        self.assertEqual(caught.exception.code, "invalid_response")

    async def test_service_failure_and_fail_closed_configuration(self):
        self.sdk.auth_configs.get = lambda *_: (_ for _ in ()).throw(OSError("private key"))
        with self.assertRaises(IntegrationError) as caught:
            await self.adapter.initiate_connection("github", 42)
        self.assertEqual(caught.exception.code, "service_error")
        self.assertNotIn("private key", str(caught.exception))
        disabled = ComposioAdapter.from_env({})
        with self.assertRaises(IntegrationError) as caught:
            await disabled.list_connections(42)
        self.assertEqual(caught.exception.code, "disabled")


class DiscordConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sdk = FakeSDK()
        self.adapter = ComposioAdapter(connection_policy(), client=self.sdk)
        self.author = SimpleNamespace(id=42, bot=False, send=AsyncMock())
        self.channel = SimpleNamespace(guild=object(), send=AsyncMock())

    async def test_intents_are_anchored(self):
        self.assertEqual(main.chat_intent("connect to Google Calendar")[1]["toolkit"],
                         "googlecalendar")
        self.assertEqual(main.chat_intent("Show my connected apps")[0], "connection")
        self.assertEqual(main.chat_intent("What permissions does my GitHub connection have?")[0],
                         "connection")
        self.assertEqual(main.chat_intent("Disconnect GitHub")[0], "connection")
        self.assertNotEqual(main.chat_intent("perhaps connect to GitHub someday")[0], "connection")

    async def test_authorized_owner_can_create_link(self):
        with patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "42"}), \
             patch.object(main, "composio_adapter", self.adapter):
            await main._handle_connection(self.author, self.channel,
                                          {"operation": "connect", "toolkit": "github"})
        self.assertEqual(len(self.sdk.connected_accounts.links), 1)
        self.assertIn("private-token-value", self.author.send.call_args.args[0])
        self.assertNotIn("private-token-value", str(self.channel.send.await_args_list))

    async def test_unauthorized_user_cannot_create_link(self):
        with patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "99"}), \
             patch.object(main, "composio_adapter", self.adapter):
            await main._handle_connection(self.author, self.channel,
                                          {"operation": "connect", "toolkit": "github"})
        self.assertEqual(len(self.sdk.connected_accounts.links), 0)
        self.assertIn("not in the configured owner allowlist",
                      self.channel.send.call_args.args[0])

    async def test_public_natural_request_uses_authorized_dm_path(self):
        message = SimpleNamespace(author=self.author, channel=self.channel,
                                  content="Max, connect to GitHub.", mentions=[])
        with patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "42"}), \
             patch.object(main, "composio_adapter", self.adapter), \
             patch.object(main, "get_pref", return_value=None), \
             patch.object(main.bot, "process_commands", new_callable=AsyncMock) as process:
            await main.on_message(message)
        self.assertEqual(len(self.sdk.connected_accounts.links), 1)
        self.assertIn("private-token-value", self.author.send.call_args.args[0])
        self.assertNotIn("private-token-value", str(self.channel.send.await_args_list))
        process.assert_awaited_once_with(message)

    async def test_link_only_in_owner_dm_and_account_status(self):
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        logger = logging.getLogger("composio_adapter")
        logger.addHandler(handler)
        with patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "42"}), \
             patch.object(main, "composio_adapter", self.adapter):
            await main._handle_connection(self.author, self.channel,
                                          {"operation": "connect", "toolkit": "github"})
            private = self.author.send.call_args.args[0]
            self.assertIn("private-token-value", private)
            self.assertNotIn("private-token-value", str(self.channel.send.await_args_list))
            await main._handle_connection(self.author, self.channel,
                                          {"operation": "status", "toolkit": "github"})
            self.assertIn("INITIALIZING", self.author.send.call_args.args[0])
            await main._handle_connection(self.author, self.channel,
                                          {"operation": "disconnect", "toolkit": "github"})
            self.assertIn("Deleted", self.author.send.call_args.args[0])
        logger.removeHandler(handler)
        self.assertNotIn("private-token-value", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
