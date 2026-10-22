"""Offline provider-boundary tests; no model server or cloud account required."""

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from model_router import ModelError, ModelRouter

MESSAGES = [{"role": "user", "content": "hello"}]


class LocalResponse:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self.data = data if data is not None else {
            "model": "qwen3:8b", "message": {"content": "hello back"},
            "done": True, "prompt_eval_count": 3, "eval_count": 2,
        }
        self.text = text

    def json(self):
        return self.data


def local_router(env=None, response=None):
    client = SimpleNamespace(post=AsyncMock(return_value=response or LocalResponse()))
    return ModelRouter(environ=env or {}, local_client=client), client


class LocalInferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_default_and_endpoint(self):
        router, client = local_router({"MAX_OLLAMA_BASE_URL": "http://localhost:12345",
                                       "MAX_MODEL_CODING": "coder-local"})
        reply = await router.generate(MESSAGES, task="coding")
        self.assertEqual(reply.text, "hello back")
        self.assertEqual(reply.prompt_tokens, 3)
        args, kwargs = client.post.call_args
        self.assertEqual(args[0], "http://localhost:12345/api/chat")
        self.assertEqual(kwargs["json"]["model"], "coder-local")
        self.assertEqual(kwargs["json"]["keep_alive"], "0")
        self.assertEqual(kwargs["json"]["options"]["num_ctx"], 8192)

    async def test_thinking_model_returns_final_content_with_short_limit(self):
        async def post(_url, *, json):
            if json.get("think") is False:
                return LocalResponse(data={
                    "model": "qwen3:8b", "message": {"role": "assistant", "content": "hello back"},
                    "done": True, "done_reason": "stop",
                })
            return LocalResponse(data={
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": "", "thinking": "private reasoning"},
                "done": True, "done_reason": "length",
            })

        router, client = local_router()
        client.post.side_effect = post
        reply = await router.generate(MESSAGES, max_tokens=32)
        self.assertEqual(reply.text, "hello back")
        self.assertIs(client.post.call_args.kwargs["json"]["think"], False)
        self.assertEqual(client.post.call_args.kwargs["json"]["options"]["num_predict"], 32)

    async def test_native_chat_response_content_validation(self):
        invalid_responses = (
            [],
            {"done": True},
            {"message": {}, "done": True},
            {"message": [], "done": True},
            {"message": {"content": None}, "done": True},
            {"message": {"content": ["text"]}, "done": True},
            {"message": {"content": ""}, "done": True},
            {"message": {"content": "", "thinking": "private reasoning"}, "done": True},
            {"message": {"content": "  "}, "done": True},
            {"message": {"content": "valid"}},
            {"message": {"content": "valid"}, "done": "true"},
        )
        for data in invalid_responses:
            with self.subTest(data=data):
                router, _ = local_router(response=LocalResponse(data=data))
                with self.assertRaises(ModelError) as caught:
                    await router.generate(MESSAGES)
                self.assertEqual(caught.exception.code, "malformed_response")

        router, _ = local_router(response=LocalResponse(data={
            "model": "qwen3:8b", "message": {"role": "assistant", "content": "  final answer  ",
                                           "thinking": "private reasoning"}, "done": True,
        }))
        self.assertEqual((await router.generate(MESSAGES)).text, "final answer")

    async def test_role_selection_and_legacy_variables(self):
        router, client = local_router({"MAX_MODEL_PLANNER": "plan", "MAX_MODEL_CODING": "code",
                                       "MAX_MODEL_REVIEW": "review", "AI_MODEL": "legacy",
                                       "MAX_MODEL_TESTER": "test"})
        self.assertEqual(router.selected_model(role="planner"), "plan")
        self.assertEqual(router.selected_model(role="coder"), "code")
        self.assertEqual(router.selected_model(role="reviewer"), "review")
        self.assertEqual(router.selected_model(role="tester"), "test")
        self.assertEqual(router.selected_model("chat"), "legacy")
        self.assertEqual(router.selected_model("vision"), "qwen3-vl:8b")
        await router.generate(MESSAGES, role="coder")
        self.assertEqual(client.post.call_args.kwargs["json"]["model"], "code")

    async def test_private_cloud_blocked_before_client_call(self):
        cloud = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
        router = ModelRouter(environ={"MAX_INFERENCE_PROVIDER": "cloud",
                                      "MAX_INFERENCE_POLICY": "cloud_allowed"},
                             cloud_client=cloud)
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, data_class="source")
        self.assertEqual(caught.exception.code, "privacy_blocked")
        cloud.chat.completions.create.assert_not_called()

    async def test_local_only_blocks_cloud_even_with_key(self):
        cloud = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
        router = ModelRouter(environ={"AI_API_KEY": "secret", "MAX_INFERENCE_POLICY": "local_only"},
                             cloud_client=cloud)
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, provider="cloud", data_class="general")
        self.assertEqual(caught.exception.code, "provider_disabled")
        self.assertNotIn("secret", str(caught.exception))
        cloud.chat.completions.create.assert_not_called()

    async def test_explicit_cloud_permission_normalizes_response(self):
        raw = SimpleNamespace(model="gateway-model", choices=[SimpleNamespace(
            message=SimpleNamespace(content="cloud answer"))],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2))
        create = AsyncMock(return_value=raw)
        cloud = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        router = ModelRouter(environ={
            "MAX_INFERENCE_PROVIDER": "cloud", "MAX_INFERENCE_POLICY": "cloud_allowed",
            "MAX_ALLOW_CLOUD_PRIVATE_CONTEXT": "true", "AI_MODEL": "legacy-id",
            "AI_BASE_URL": "https://freellm.example/v1",
        }, cloud_client=cloud)
        reply = await router.generate(MESSAGES, data_class="memory")
        self.assertEqual((reply.text, reply.model, reply.prompt_tokens),
                         ("cloud answer", "gateway-model", 4))
        self.assertEqual(create.call_args.kwargs["model"], "legacy-id")

    async def test_cloud_request_redacts_credentials_even_when_permitted(self):
        raw = SimpleNamespace(model="cloud", choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok"))], usage=None)
        create = AsyncMock(return_value=raw)
        cloud = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        router = ModelRouter(environ={"MAX_INFERENCE_POLICY": "cloud_allowed",
                                      "MAX_ALLOW_CLOUD_SOURCE": "true",
                                      "AI_BASE_URL": "https://freellm.example/v1",
                                      "AI_MODEL": "auto"}, cloud_client=cloud)
        await router.generate([{"role": "user", "content": "api_key=private-value, token sk-abcdefghijklmno"}],
                              provider="cloud", data_class="source")
        forwarded = create.call_args.kwargs["messages"][0]["content"]
        self.assertNotIn("private-value", forwarded)
        self.assertNotIn("sk-abcdefghijklmno", forwarded)

    async def test_class_specific_cloud_permission(self):
        raw = SimpleNamespace(model="cloud", choices=[SimpleNamespace(
            message=SimpleNamespace(content="allowed"))], usage=None)
        create = AsyncMock(return_value=raw)
        cloud = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        router = ModelRouter(environ={
            "MAX_INFERENCE_POLICY": "cloud_allowed",
            "MAX_ALLOW_CLOUD_SOURCE": "true",
            "AI_BASE_URL": "https://freellm.example/v1", "AI_MODEL": "auto",
        }, cloud_client=cloud)
        self.assertEqual((await router.generate(MESSAGES, provider="cloud",
                                                 data_class="source")).text, "allowed")
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, provider="cloud", data_class="memory")
        self.assertEqual(caught.exception.code, "privacy_blocked")
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, provider="cloud", data_class=("source", "memory"))
        self.assertEqual(caught.exception.code, "privacy_blocked")
        self.assertEqual(create.await_count, 1)

    async def test_no_cloud_fallback_on_missing_model_or_downtime(self):
        for response in (LocalResponse(404), LocalResponse(503)):
            router, _ = local_router({"MAX_INFERENCE_POLICY": "cloud_allowed"}, response)
            with self.assertRaises(ModelError) as caught:
                await router.generate(MESSAGES)
            self.assertIn(caught.exception.code, ("missing_model", "unavailable"))

        cloud_create = AsyncMock()
        cloud = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=cloud_create)))
        local = SimpleNamespace(post=AsyncMock(side_effect=OSError("private diagnostic")))
        router = ModelRouter(environ={"MAX_INFERENCE_POLICY": "cloud_allowed",
                                      "MAX_ALLOW_CLOUD_PRIVATE_CONTEXT": "true"},
                             local_client=local, cloud_client=cloud)
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, data_class="source")
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertNotIn("private diagnostic", str(caught.exception))
        cloud_create.assert_not_called()

    async def test_timeout_and_malformed_response(self):
        async def hanging(*args, **kwargs):
            await asyncio.sleep(0.1)
        router, client = local_router({"MAX_MODEL_TIMEOUT_SECONDS": "1"})
        client.post.side_effect = hanging
        router.config = replace(router.config, timeout_seconds=0.01)
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES)
        self.assertEqual(caught.exception.code, "timeout")
        router, _ = local_router(response=LocalResponse(data={"message": {}}))
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES)
        self.assertEqual(caught.exception.code, "malformed_response")

    async def test_context_limit_and_rate_limit_are_safe(self):
        router, _ = local_router({"MAX_MODEL_INPUT_CHARS": "2"})
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES)
        self.assertEqual(caught.exception.code, "context_limit")
        router, _ = local_router(response=LocalResponse(400, text="context length exceeded: PRIVATE"))
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES)
        self.assertEqual(caught.exception.code, "context_limit")
        self.assertNotIn("PRIVATE", str(caught.exception))
        class RateLimit(Exception):
            status_code = 429
        create = AsyncMock(side_effect=RateLimit("key=secret"))
        cloud = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        router = ModelRouter(environ={"MAX_INFERENCE_POLICY": "cloud_allowed",
                                     "AI_BASE_URL": "https://freellm.example/v1", "AI_MODEL": "auto"}, cloud_client=cloud)
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, provider="cloud", data_class="general")
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertNotIn("secret", str(caught.exception))

    async def test_remote_ollama_endpoint_is_not_local(self):
        router, client = local_router({"MAX_OLLAMA_BASE_URL": "https://ollama.com"})
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES)
        self.assertEqual(caught.exception.code, "privacy_blocked")
        client.post.assert_not_called()

    async def test_cloud_malformed_response_is_safe(self):
        create = AsyncMock(return_value=SimpleNamespace(choices=[]))
        cloud = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        router = ModelRouter(environ={"MAX_INFERENCE_POLICY": "cloud_allowed",
                                     "AI_BASE_URL": "https://freellm.example/v1", "AI_MODEL": "auto"}, cloud_client=cloud)
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, provider="cloud", data_class="public")
        self.assertEqual(caught.exception.code, "malformed_response")

    async def test_default_concurrency_one(self):
        active = 0
        peak = 0
        async def post(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return LocalResponse()
        router, client = local_router()
        client.post.side_effect = post
        await asyncio.gather(*(router.generate(MESSAGES) for _ in range(4)))
        self.assertEqual(peak, 1)

    async def test_configured_concurrency_two(self):
        active = 0
        peak = 0
        async def post(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return LocalResponse()
        router, client = local_router({"MAX_LOCAL_INFERENCE_CONCURRENCY": "2"})
        client.post.side_effect = post
        await asyncio.gather(*(router.generate(MESSAGES) for _ in range(4)))
        self.assertEqual(peak, 2)


if __name__ == "__main__":
    unittest.main()
