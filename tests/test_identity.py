"""Identity and compatibility checks for the Discord entry points."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import main
import integrations
import usage
from free_sandbox import BRAIN_HEADER


class IdentityTests(unittest.TestCase):
    def test_new_and_existing_chat_triggers(self):
        for trigger in ("max help", "hey Max, help", "mex help", "flanx help"):
            with self.subTest(trigger=trigger):
                self.assertEqual(main._strip_name_trigger(trigger), "help")

    def test_prefix_commands_keep_legacy_aliases(self):
        for new, aliases in (("max_fix", ("mex_fix", "flanx_fix")),
                             ("max_brain", ("mex_brain", "flanx_brain")),
                             ("max_stats", ("mex_stats", "flanx_stats"))):
            with self.subTest(command=new):
                for existing in aliases:
                    self.assertIs(main.bot.get_command(new), main.bot.get_command(existing))
        self.assertIsNotNone(main.bot.get_command("update_repo"))

    def test_visible_identity_is_max(self):
        for text in (main.CHAT_HELP, main.PERSONA_CARD, main.SYSTEM_PROMPT, BRAIN_HEADER):
            with self.subTest(text=text[:30]):
                self.assertIn("Max", text)
                self.assertNotIn("MEX", text)
        self.assertIn("I'm Max", main.PERSONA_CARD)

    def test_new_task_manifest_uses_max_identity(self):
        manifest = main.build_task_manifest(123, "owner/repo", "app.py", "Fix a bug", "main", True)
        self.assertIn("# Max Automated Update Manifest #123", manifest)
        self.assertIn("`max-evolution-123`", manifest)
        self.assertNotIn("MEX", manifest)

    def test_usage_and_integration_examples_use_max(self):
        with patch.object(usage, "get_usage", return_value=usage._empty()):
            report = usage.format_usage()
        self.assertIn("Max usage", report)
        self.assertNotIn("MEX", report)

        examples = "\n".join(
            example
            for _, _, phrases in integrations.catalog()
            for example in phrases
        )
        self.assertIn("max ", examples)
        self.assertNotIn("MEX", examples)


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_general_chat_uses_task_engine_and_existing_reply_path(self):
        replies = []

        class Channel:
            @asynccontextmanager
            async def typing(self):
                yield

            async def send(self, reply):
                replies.append(reply)

        message = SimpleNamespace(
            content="max tell me a joke", mentions=[], channel=Channel(),
            author=SimpleNamespace(bot=False, id=42),
        )
        with (patch.object(main, "get_pref", return_value=None),
              patch.object(main, "generate_assistant_reply", new_callable=AsyncMock,
                           return_value="A short reply") as model_reply,
              patch.object(main.bot, "process_commands", new_callable=AsyncMock) as commands):
            await main.on_message(message)
        model_reply.assert_awaited_once_with("tell me a joke", task="general")
        commands.assert_awaited_once_with(message)
        self.assertEqual(replies, ["A short reply"])


if __name__ == "__main__":
    unittest.main()
