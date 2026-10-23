"""Discord entry points use the shared router and handle safe provider failures."""

import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from model_router import ModelError, ModelReply, ModelRouter
from task_engine import Orchestrator, Status, Task


class MainInferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_uses_specific_route(self):
        fake = SimpleNamespace(generate=AsyncMock(return_value=ModelReply("hello", "qwen3:8b")))
        with patch.object(main, "model_router", fake):
            self.assertEqual(await main.generate_assistant_reply("hi"), "hello")
        self.assertEqual(fake.generate.call_args.kwargs["data_class"], "chat")
        self.assertEqual(fake.generate.call_args.kwargs["task"], "general")
        self.assertEqual(main.discord_data_class("please inspect owner/repo"), "private_repo")
        self.assertEqual(main.discord_data_class("```python\nprint(1)\n```"), "source")

    async def test_chat_model_outage_does_not_escape(self):
        fake = SimpleNamespace(generate=AsyncMock(side_effect=ModelError(
            "unavailable", "The local model server is unavailable. Check Ollama.")))
        with patch.object(main, "model_router", fake):
            reply = await main.generate_assistant_reply("hi")
        self.assertIn("unavailable", reply)

    async def test_ordinary_discord_chat_reaches_permitted_freellm(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="cloud hello"))], model="auto", usage=None)
        create = AsyncMock(return_value=response)
        cloud = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        router = ModelRouter(environ={"MAX_INFERENCE_PROVIDER": "freellm",
                                      "MAX_INFERENCE_POLICY": "cloud_allowed",
                                      "MAX_ALLOW_CLOUD_CHAT": "true",
                                      "AI_BASE_URL": "https://freellm.example/v1",
                                      "AI_MODEL": "auto"}, cloud_client=cloud)
        with patch.object(main, "model_router", router):
            self.assertEqual(await main.generate_assistant_reply("Explain this idea"), "cloud hello")
            blocked = await main.generate_assistant_reply("Explain owner/private-repo")
        self.assertIn("Privacy policy blocks FreeLLM", blocked)
        self.assertEqual(create.await_count, 1)

    async def test_task_status_fails_with_safe_provider_diagnostic(self):
        fake = SimpleNamespace(generate=AsyncMock(side_effect=ModelError(
            "missing_credentials", "FreeLLM requires AI_API_KEY.")))
        with tempfile.TemporaryDirectory() as folder, patch.object(main, "model_router", fake):
            engine = Orchestrator(store_path=folder + "/tasks.db")
            engine.register("general_chat", "model.reply", main._general_chat_handler,
                            valid_input=lambda p: isinstance(p, str),
                            valid_result=lambda r: isinstance(r, str) and bool(r))
            task = Task.new("General chat response via FreeLLM", "general_chat",
                            permitted_tools={"model.reply"})
            outcome = await engine.run(task, "hello")
        self.assertEqual(outcome.task.status, Status.FAILED)
        self.assertEqual(outcome.task.error_code, "model_missing_credentials")
        self.assertIn("AI_API_KEY", outcome.error)

    async def test_patch_uses_source_route(self):
        fake = SimpleNamespace(generate=AsyncMock(return_value=ModelReply(
            "print('ready')", "qwen3:8b")))
        with patch.object(main, "model_router", fake):
            patch_text = await main.generate_patch(
                "print('old')", "app.py", "main", "", "", "", "change output",
                model="qwen3:8b",
            )
        self.assertEqual(patch_text, "print('ready')")
        self.assertEqual(fake.generate.call_args.kwargs["data_class"], ("source", "memory"))
        self.assertEqual(fake.generate.call_args.kwargs["task"], "coding")


if __name__ == "__main__":
    unittest.main()
