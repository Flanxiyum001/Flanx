"""Regression tests for the 2026-10-24 runtime feature merge.

All Discord and network interactions are offline mocks.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from model_router import ModelRouter


def fake_message(content, *, attachments=None):
    return SimpleNamespace(
        content=content, attachments=attachments or [], mentions=[],
        author=SimpleNamespace(id=42, bot=False),
        guild=SimpleNamespace(id=100),
        channel=SimpleNamespace(send=AsyncMock(), id=10),
    )


class DiscordRoutingRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unaddressed_message_does_not_trigger_multimodal_or_help(self):
        for content, attachments in (
            ("normal conversation between users", [object()]),
            ("!max_composio_status", [object()]),
            ("regular chat", []),
        ):
            with self.subTest(content=content):
                message = fake_message(content, attachments=attachments)
                with patch.object(main.bot, "process_commands", new_callable=AsyncMock) as process, \
                     patch.object(main, "handle_discord_attachments", new_callable=AsyncMock) as analyze:
                    await main.on_message(message)
                analyze.assert_not_awaited()
                message.channel.send.assert_not_awaited()
                process.assert_awaited_once_with(message)

    async def test_proactive_uses_existing_proactive_engine_method(self):
        message = fake_message("max proactive refactor auth code")
        with patch.object(main.bot, "process_commands", new_callable=AsyncMock), \
             patch.object(main, "get_pref", return_value=None), \
             patch.object(main, "github_user_allowed", return_value=True), \
             patch.object(main.proactive_engine, "generate_proactive_message", return_value="Consider adding tests") as make:
            await main.on_message(message)
        make.assert_called_once()
        self.assertEqual(message.channel.send.await_args.args[0], "Consider adding tests")

    async def test_insights_keyword_routes_to_insights(self):
        self.assertEqual(main.chat_intent("insights")[0], "insights")
        self.assertEqual(main.chat_intent("my history")[0], "insights")
        message = fake_message("max insights")
        with patch.object(main.bot, "process_commands", new_callable=AsyncMock), \
             patch.object(main, "get_pref", return_value=None), \
             patch.object(main, "github_user_allowed", return_value=True), \
             patch.object(main.proactive_engine, "get_personalized_insights", return_value="Owner insights"):
            await main.on_message(message)
        self.assertEqual(message.channel.send.await_args.args[0], "Owner insights")

    async def test_private_knowledge_requires_github_user_allowlist(self):
        for content in ("max insights", "max proactive refactor auth"):
            with self.subTest(content=content):
                message = fake_message(content)
                with patch.object(main.bot, "process_commands", new_callable=AsyncMock), \
                     patch.object(main, "get_pref", return_value=None), \
                     patch.object(main, "github_user_allowed", return_value=False), \
                     patch.object(main.proactive_engine, "get_personalized_insights") as read, \
                     patch.object(main.proactive_engine, "generate_proactive_message") as suggest:
                    await main.on_message(message)
                read.assert_not_called()
                suggest.assert_not_called()
                self.assertEqual(message.channel.send.await_args.args[0], main.GITHUB_ACCESS_DENIED)

    async def test_background_command_does_not_create_phantom_tasks(self):
        message = fake_message("max background research railway")
        with patch.object(main.bot, "process_commands", new_callable=AsyncMock), \
             patch.object(main, "get_pref", return_value=None), \
             patch.object(main.notification_system, "create_task") as create:
            await main.on_message(message)
        create.assert_not_called()
        self.assertIn("No task was started", message.channel.send.await_args.args[0])

        ctx = SimpleNamespace(author=SimpleNamespace(id=42),
                              channel=SimpleNamespace(id=10), send=AsyncMock())
        with patch.object(main.notification_system, "create_task") as create:
            await main.max_background.callback(ctx, description="compile")
        create.assert_not_called()
        self.assertIn("No task was started", ctx.send.await_args.args[0])

    async def test_background_status_cannot_reveal_other_users_task(self):
        task = SimpleNamespace(task_id="bg_private", user_id=99, status="pending",
                               created_at="2026-01-01T00:00")
        ctx = SimpleNamespace(author=SimpleNamespace(id=42), send=AsyncMock())
        with patch.object(main.notification_system, "get_task", return_value=task):
            await main.max_bg_status.callback(ctx, "bg_private")
        self.assertIn("Unknown background task", ctx.send.await_args.args[0])
        self.assertNotIn("pending", ctx.send.await_args.args[0])


class VisionModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ollama_accepts_image_payload_without_cloud_fallback(self):
        def reply():
            return {"message": {"content": "Found a layout issue"}, "done": True}
        local = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(
            status_code=200, text="", json=reply)))
        cloud = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock())))
        router = ModelRouter(environ={"MAX_INFERENCE_PROVIDER": "nvidia",
                                      "MAX_MODEL_VISION": "qwen3-vl:8b"},
                             local_client=local, cloud_client=cloud, nvidia_client=cloud)
        answer = await router.generate([
            {"role": "system", "content": "Analyze the screenshot."},
            {"role": "user", "content": "A UI screenshot", "images": ["aGVsbG8="]},
        ], task="vision", provider="local", data_class="source")
        self.assertEqual(answer.provider, "ollama")
        self.assertEqual(answer.text, "Found a layout issue")
        args = local.post.await_args.kwargs["json"]
        self.assertEqual(args["model"], "qwen3-vl:8b")
        self.assertEqual(args["messages"][1]["images"], ["aGVsbG8="])
        cloud.chat.completions.create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
