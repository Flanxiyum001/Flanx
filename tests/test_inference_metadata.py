"""Router metadata records actual adapter outcomes without prompt or key logs."""

import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from model_router import ModelError, ModelRouter


MESSAGES = [{"role": "user", "content": "private test phrase"}]
BASE = {"AI_BASE_URL": "https://freellm.example/v1", "AI_MODEL": "auto",
        "AI_API_KEY": "synthetic-private-key", "MAX_INFERENCE_POLICY": "cloud_allowed",
        "MAX_ALLOW_CLOUD_CHAT": "true"}


def cloud_client():
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="gateway answer"))], model="opaque-gateway-model", usage=None)
    create = AsyncMock(return_value=response)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), create


def local_client():
    response = SimpleNamespace(status_code=200,
        json=lambda: {"message": {"content": "local answer"}, "done": True,
                      "model": "qwen3:8b"})
    post = AsyncMock(return_value=response)
    return SimpleNamespace(post=post), post


class InferenceMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_freellm_uses_gateway_adapter_and_unknown_upstream(self):
        cloud, create = cloud_client()
        local, post = local_client()
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "freellm"},
                             local_client=local, cloud_client=cloud)
        with self.assertLogs("model_router", level="INFO") as captured:
            reply = await router.generate(MESSAGES, data_class="chat")
        info = reply.metadata
        self.assertRegex(info.request_id, r"\A[0-9a-f]{32}\Z")
        self.assertEqual((info.requested_provider, info.selected_provider,
                          info.configured_model, info.status),
                         ("freellm", "freellm", "auto", "success"))
        self.assertEqual(info.upstream_provider, "unknown")
        self.assertFalse(info.fallback_attempted)
        self.assertFalse(info.fallback_used)
        self.assertEqual(router.inference_status(info.request_id), info)
        create.assert_awaited_once()
        post.assert_not_called()
        logs = "\n".join(captured.output)
        self.assertIn(info.request_id, logs)
        self.assertNotIn("private test phrase", logs)
        self.assertNotIn("synthetic-private-key", logs)
        self.assertNotIn("opaque-gateway-model", logs)

    async def test_explicit_local_reports_only_local_adapter(self):
        cloud, create = cloud_client()
        local, post = local_client()
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "local"},
                             local_client=local, cloud_client=cloud)
        reply = await router.generate(MESSAGES, data_class="chat")
        self.assertEqual(reply.metadata.requested_provider, "local")
        self.assertEqual(reply.metadata.selected_provider, "local")
        self.assertEqual(reply.metadata.status, "success")
        self.assertEqual(reply.metadata.upstream_provider, "not_applicable")
        self.assertFalse(reply.metadata.fallback_attempted)
        post.assert_awaited_once()
        create.assert_not_called()

    async def test_failed_provider_is_never_success(self):
        cloud, create = cloud_client()
        class Unauthorized(Exception):
            status_code = 401
        create.side_effect = Unauthorized("private response body")
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "freellm"},
                             cloud_client=cloud)
        with self.assertLogs("model_router", level="INFO") as captured:
            with self.assertRaises(ModelError) as caught:
                await router.generate(MESSAGES, data_class="chat")
        info = caught.exception.metadata
        self.assertEqual((info.status, info.selected_provider, info.error_category),
                         ("failure", "freellm", "authentication"))
        self.assertFalse(info.fallback_used)
        self.assertEqual(router.inference_status(info.request_id), info)
        self.assertNotIn("private response body", "\n".join(captured.output))

    async def test_fallback_records_attempt_and_actual_success(self):
        cloud, create = cloud_client()
        local, post = local_client()
        post.side_effect = OSError("private local detail")
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "auto",
                                     "MAX_AUTO_CLOUD_FALLBACK": "true"},
                             local_client=local, cloud_client=cloud)
        reply = await router.generate(MESSAGES, data_class="chat")
        info = reply.metadata
        self.assertEqual((info.requested_provider, info.selected_provider,
                          info.fallback_attempted, info.fallback_used, info.status),
                         ("auto", "freellm", True, True, "success"))
        post.assert_awaited_once()
        create.assert_awaited_once()

    async def test_blocked_fallback_keeps_local_failure_metadata(self):
        cloud, create = cloud_client()
        local, post = local_client()
        post.side_effect = OSError("offline")
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "auto",
                                     "MAX_AUTO_CLOUD_FALLBACK": "true",
                                     "MAX_ALLOW_CLOUD_CHAT": "false"},
                             local_client=local, cloud_client=cloud)
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, data_class="chat")
        info = caught.exception.metadata
        self.assertEqual((info.status, info.selected_provider), ("failure", "local"))
        self.assertFalse(info.fallback_attempted)
        self.assertFalse(info.fallback_used)
        create.assert_not_called()

    async def test_failed_fallback_records_last_attempt_without_success(self):
        cloud, create = cloud_client()
        create.side_effect = OSError("private cloud detail")
        local, post = local_client()
        post.side_effect = OSError("private local detail")
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "auto",
                                     "MAX_AUTO_CLOUD_FALLBACK": "true"},
                             local_client=local, cloud_client=cloud)
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, data_class="chat")
        info = caught.exception.metadata
        self.assertEqual((info.status, info.selected_provider, info.error_category),
                         ("failure", "freellm", "providers_failed"))
        self.assertTrue(info.fallback_attempted)
        self.assertFalse(info.fallback_used)

    async def test_admin_diagnostic_matches_request_id_not_model_claim(self):
        cloud, _ = cloud_client()
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "freellm"},
                             cloud_client=cloud)
        with patch.object(main, "model_router", router):
            response = await main.generate_assistant_reply("Explain this", include_request_id=True)
            request_id = re.search(r"Request ID: `([0-9a-f]{32})`", response).group(1)
            ctx = SimpleNamespace(guild=None,
                author=SimpleNamespace(id=42), send=AsyncMock())
            with patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "42"}):
                await main.max_inference_status.callback(ctx, request_id)
        diagnostic = ctx.send.await_args.args[0]
        self.assertIn(request_id, diagnostic)
        self.assertIn("FreeLLM gateway", diagnostic)
        self.assertIn("upstream provider: `unknown`", diagnostic)
        self.assertNotIn("opaque-gateway-model", diagnostic)
        self.assertNotIn("private test phrase", diagnostic)
        ctx.send.reset_mock()
        with patch.dict("os.environ", {"MAX_OWNER_DISCORD_IDS": "99"}):
            await main.max_inference_status.callback(ctx, request_id)
        self.assertIn("not in the configured owner allowlist",
                      ctx.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
