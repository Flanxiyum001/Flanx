"""Discord entry points use the shared router and handle safe provider failures."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from model_router import ModelError, ModelReply


class MainInferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_uses_private_route(self):
        fake = SimpleNamespace(generate=AsyncMock(return_value=ModelReply("hello", "qwen3:8b")))
        with patch.object(main, "model_router", fake):
            self.assertEqual(await main.generate_assistant_reply("hi"), "hello")
        self.assertEqual(fake.generate.call_args.kwargs["data_class"], "private_repo")
        self.assertEqual(fake.generate.call_args.kwargs["task"], "general")

    async def test_chat_model_outage_does_not_escape(self):
        fake = SimpleNamespace(generate=AsyncMock(side_effect=ModelError(
            "unavailable", "The local model server is unavailable. Check Ollama.")))
        with patch.object(main, "model_router", fake):
            reply = await main.generate_assistant_reply("hi")
        self.assertIn("unavailable", reply)

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
