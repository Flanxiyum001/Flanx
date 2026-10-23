"""Offline checks for explicit FreeLLM routing and consent-gated fallback."""

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from model_router import ModelError, ModelRouter


MESSAGES = [{"role": "user", "content": "hello"}]
BASE = {"AI_BASE_URL": "https://max-freellm.up.railway.app/v1",
        "AI_MODEL": "auto", "AI_API_KEY": "synthetic-key",
        "MAX_INFERENCE_POLICY": "cloud_allowed"}


def cloud_client(reply="FreeLLM answer"):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
        model="auto", usage=None,
    )
    create = AsyncMock(return_value=response)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), create


class ProviderSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_freellm_ignores_offline_ollama(self):
        cloud, create = cloud_client()
        local = SimpleNamespace(post=AsyncMock(side_effect=OSError("Ollama offline")))
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "freellm"},
                             local_client=local, cloud_client=cloud)
        with self.assertRaises(ModelError) as blocked:
            await router.generate(MESSAGES, data_class="chat")
        self.assertEqual(blocked.exception.code, "privacy_blocked")
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "freellm",
                                     "MAX_ALLOW_CLOUD_CHAT": "true"},
                             local_client=local, cloud_client=cloud)
        reply = await router.generate(MESSAGES, data_class="chat")
        self.assertEqual((reply.text, reply.provider), ("FreeLLM answer", "freellm"))
        self.assertEqual(create.call_args.kwargs["model"], "auto")
        local.post.assert_not_called()
        override = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "local"},
                               local_client=local, cloud_client=cloud)
        self.assertEqual((await override.generate(MESSAGES, provider="freellm",
                                                  data_class="general")).provider, "freellm")
        local.post.assert_not_called()

    async def test_explicit_local_never_uses_freellm(self):
        cloud, create = cloud_client()
        local = SimpleNamespace(post=AsyncMock(side_effect=OSError("secret endpoint detail")))
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "local"},
                             local_client=local, cloud_client=cloud)
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, data_class="general")
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertNotIn("secret endpoint", str(caught.exception))
        create.assert_not_called()

    async def test_auto_fallback_requires_flag_and_class_permission(self):
        local = SimpleNamespace(post=AsyncMock(side_effect=OSError("offline")))
        cloud, create = cloud_client()
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "auto"},
                             local_client=local, cloud_client=cloud)
        with self.assertRaises(ModelError):
            await router.generate(MESSAGES, data_class="general")
        create.assert_not_called()

        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "auto",
                                     "MAX_AUTO_CLOUD_FALLBACK": "true"},
                             local_client=local, cloud_client=cloud)
        reply = await router.generate(MESSAGES, data_class="general")
        self.assertEqual(reply.provider, "freellm")
        self.assertEqual(create.await_count, 1)
        self.assertEqual(local.post.call_args.kwargs["json"]["model"], "qwen3:8b")

        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, data_class=("source", "memory"))
        self.assertIn("fallback blocked", str(caught.exception))
        self.assertEqual(create.await_count, 1)

    async def test_explicit_freellm_refuses_private_data_and_credential_fields(self):
        cloud, create = cloud_client()
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "freellm"},
                             cloud_client=cloud)
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, data_class="private_repo")
        self.assertEqual(caught.exception.code, "privacy_blocked")
        with self.assertRaises(ModelError) as caught:
            await router.generate([{"role": "user", "content": "private source sk-secretvalue123456"}],
                                  data_class="source")
        self.assertEqual(caught.exception.code, "privacy_blocked")
        create.assert_not_called()
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "freellm",
                                     "MAX_ALLOW_CLOUD_SOURCE": "true"}, cloud_client=cloud)
        with self.assertRaises(ModelError) as caught:
            await router.generate([{"role": "user", "content": "api_key=private-value"}],
                                  data_class="source")
        self.assertEqual(caught.exception.code, "privacy_blocked")
        self.assertIn("Credentials", str(caught.exception))
        create.assert_not_called()
        private_router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "freellm",
                                             "MAX_ALLOW_CLOUD_PRIVATE_REPO": "true"},
                                     cloud_client=cloud)
        self.assertEqual((await private_router.generate(MESSAGES, data_class="private_repo")).provider,
                         "freellm")
        with self.assertRaises(ModelError):
            await private_router.generate(MESSAGES, data_class="chat")
        for secret in ("password=synthetic-secret", "Bearer synthetic-token-12345",
                       "ghp_abcdefghijklmno1234", "AI_API_KEY=synthetic-secret",
                       '{"access_token":"synthetic-secret"}',
                       "my password is synthetic-secret"):
            with self.subTest(secret=secret), self.assertRaises(ModelError):
                await private_router.generate([{"role": "user", "content": secret}],
                                              data_class="private_repo")
        self.assertEqual(create.await_count, 1)

    async def test_configuration_and_both_provider_failures_are_specific(self):
        no_key = ModelRouter(environ={"MAX_INFERENCE_PROVIDER": "freellm",
                                         "MAX_INFERENCE_POLICY": "cloud_allowed",
                                         "AI_BASE_URL": BASE["AI_BASE_URL"], "AI_MODEL": "auto"})
        with self.assertRaises(ModelError) as caught:
            await no_key.generate(MESSAGES, data_class="general")
        self.assertEqual(caught.exception.code, "missing_credentials")
        self.assertIn("AI_API_KEY", str(caught.exception))

        bad_url = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "freellm",
                                           "AI_BASE_URL": "not-an-api-url"})
        with self.assertRaises(ModelError) as caught:
            await bad_url.generate(MESSAGES, data_class="general")
        self.assertIn("AI_BASE_URL", str(caught.exception))

        class Unauthorized(Exception):
            status_code = 401
        rejected, create_rejected = cloud_client()
        create_rejected.side_effect = Unauthorized("private upstream body")
        explicit = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "freellm"},
                               cloud_client=rejected)
        with self.assertRaises(ModelError) as caught:
            await explicit.generate(MESSAGES, data_class="general")
        self.assertEqual(caught.exception.code, "authentication")
        self.assertNotIn("private upstream", str(caught.exception))

        cloud, create = cloud_client()
        create.side_effect = OSError("private connection data")
        local = SimpleNamespace(post=AsyncMock(side_effect=OSError("private local data")))
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "auto",
                                     "MAX_AUTO_CLOUD_FALLBACK": "true"},
                             local_client=local, cloud_client=cloud)
        with self.assertRaises(ModelError) as caught:
            await router.generate(MESSAGES, data_class="general")
        self.assertEqual(caught.exception.code, "providers_failed")
        self.assertIn("Ollama:", str(caught.exception))
        self.assertIn("FreeLLM:", str(caught.exception))
        self.assertNotIn("private connection", str(caught.exception))

    async def test_logs_contain_route_without_prompt_or_key(self):
        cloud, _ = cloud_client()
        router = ModelRouter(environ={**BASE, "MAX_INFERENCE_PROVIDER": "freellm"},
                             cloud_client=cloud)
        with self.assertLogs("model_router", level=logging.INFO) as captured:
            await router.generate([{"role": "user", "content": "do not log this private phrase"}],
                                  data_class="general")
        output = "\n".join(captured.output)
        self.assertIn("provider=freellm", output)
        self.assertNotIn("private phrase", output)
        self.assertNotIn("synthetic-key", output)


if __name__ == "__main__":
    unittest.main()
