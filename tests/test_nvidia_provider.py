"""Offline NVIDIA routing tests. No live keys, HTTP calls, or deployment."""
import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from model_router import ModelError, ModelRouter, DEFAULT_NVIDIA_MODEL


ENV = {"MAX_INFERENCE_PROVIDER": "nvidia", "MAX_INFERENCE_POLICY": "cloud_allowed",
       "NVIDIA_API_KEY": "synthetic-not-a-key", "MAX_ALLOW_CLOUD_CHAT": "true"}
PROMPT = [{"role": "user", "content": "hello"}]


def client(text="NVIDIA answer", error=None):
    result = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=text, reasoning_content="internal synthetic reasoning"))],
        model=DEFAULT_NVIDIA_MODEL,
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5))
    create = AsyncMock(side_effect=error) if error else AsyncMock(return_value=result)
    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))), create


class NvidiaProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_default_unchanged(self):
        self.assertEqual(ModelRouter(environ={}).config.provider, "local")

    async def test_response_request_and_diagnostic_metadata(self):
        stub, create = client()
        router = ModelRouter(environ=ENV, nvidia_client=stub)
        with self.assertLogs("model_router", logging.INFO) as log:
            reply = await router.generate(PROMPT, data_class="chat", task="coding",
                                          max_tokens=400, temperature=0.1)
        self.assertEqual(reply.text, "NVIDIA answer")
        self.assertEqual(reply.provider, "nvidia")
        self.assertEqual(reply.model, DEFAULT_NVIDIA_MODEL)
        self.assertEqual((reply.prompt_tokens, reply.completion_tokens), (3, 5))
        self.assertEqual(reply.metadata.selected_provider, "nvidia")
        self.assertEqual(reply.metadata.configured_model, DEFAULT_NVIDIA_MODEL)
        self.assertEqual(router.route_description(), "NVIDIA Nemotron (explicit opt-in)")
        args = create.await_args.kwargs
        self.assertEqual(args["messages"], PROMPT)
        self.assertEqual(args["model"], DEFAULT_NVIDIA_MODEL)
        self.assertEqual(args["max_tokens"], 400)
        self.assertEqual(args["temperature"], 0.1)
        self.assertIs(args["stream"], False)
        self.assertEqual(args["extra_body"], {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertNotIn("api_key", args)
        logged = " ".join(log.output)
        self.assertNotIn("synthetic-not-a-key", logged)
        self.assertNotIn("internal synthetic reasoning", logged)

    async def test_missing_key_fails_even_with_injected_client(self):
        stub, create = client()
        router = ModelRouter(environ={k: v for k, v in ENV.items()
                                      if k != "NVIDIA_API_KEY"}, nvidia_client=stub)
        with self.assertRaises(ModelError) as caught:
            await router.generate(PROMPT, data_class="chat")
        self.assertEqual(caught.exception.code, "missing_credentials")
        create.assert_not_called()

    async def test_private_classes_and_credentials_block_before_request(self):
        stub, create = client()
        for override, data_class, prompt in (
            ({"MAX_INFERENCE_POLICY": "local_only"}, "chat", PROMPT),
            ({"MAX_ALLOW_CLOUD_CHAT": "false"}, "chat", PROMPT),
            ({}, "memory", PROMPT),
            ({}, "private_repo", PROMPT),
            ({}, "source", PROMPT),
            ({}, "chat", [{"role": "user", "content": "NVIDIA_API_KEY=example-private"}]),
        ):
            with self.subTest(override=override, data_class=data_class):
                router = ModelRouter(environ={**ENV, **override}, nvidia_client=stub)
                with self.assertRaises(ModelError) as caught:
                    await router.generate(prompt, data_class=data_class)
                self.assertIn(caught.exception.code, {"provider_disabled", "privacy_blocked"})
        create.assert_not_called()

    async def test_private_optin_is_per_class(self):
        stub, create = client()
        router = ModelRouter(environ={**ENV, "MAX_ALLOW_CLOUD_SOURCE": "true"},
                             nvidia_client=stub)
        self.assertEqual((await router.generate(PROMPT, data_class="source")).provider, "nvidia")
        with self.assertRaises(ModelError):
            await router.generate(PROMPT, data_class="memory")
        self.assertEqual(create.await_count, 1)

    async def test_invalid_base_url_and_model(self):
        stub, create = client()
        for change in ({"NVIDIA_BASE_URL": "http://example.com/v1"},
                       {"NVIDIA_BASE_URL": "https://user:pass@example.com/v1"},
                       {"NVIDIA_MODEL": "model id with spaces"}):
            with self.subTest(change=change):
                with self.assertRaises(ModelError) as caught:
                    await ModelRouter(environ={**ENV, **change},
                                      nvidia_client=stub).generate(PROMPT, data_class="chat")
                self.assertEqual(caught.exception.code, "configuration")
        create.assert_not_called()

    async def test_provider_errors_no_sensitive_upstream_body(self):
        class Rejected(Exception):
            status_code = 400
        class NoAccess(Exception):
            status_code = 401
        class Limited(Exception):
            status_code = 429
        cases = [(asyncio.TimeoutError(), "timeout"), (Rejected("secret body"), "rejected"),
                 (NoAccess("secret body"), "authentication"),
                 (Limited("secret body"), "rate_limited"),
                 (OSError("secret network address"), "unavailable")]
        for error, code in cases:
            with self.subTest(code=code):
                stub, create = client(error=error)
                router = ModelRouter(environ=ENV, nvidia_client=stub)
                with self.assertRaises(ModelError) as caught:
                    await router.generate(PROMPT, data_class="chat")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("secret", str(caught.exception))
                create.assert_awaited_once()

    async def test_malformed_and_empty_completion(self):
        for text in (None, "", "  "):
            with self.subTest(text=text):
                stub, _ = client(text=text)
                with self.assertRaises(ModelError) as caught:
                    await ModelRouter(environ=ENV, nvidia_client=stub).generate(
                        PROMPT, data_class="chat")
                self.assertEqual(caught.exception.code, "malformed_response")

    async def test_no_implicit_ollama_fallback(self):
        stub, create = client()
        local = SimpleNamespace(post=AsyncMock(side_effect=OSError("local down")))
        router = ModelRouter(environ=ENV, nvidia_client=stub, local_client=local)
        await router.generate(PROMPT, data_class="chat")
        local.post.assert_not_awaited()
        create.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
