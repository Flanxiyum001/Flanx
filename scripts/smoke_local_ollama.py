"""Opt-in local Ollama smoke test. Never selects or falls back to cloud."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_router import ModelError, ModelRouter, RouterConfig  # noqa: E402


async def smoke() -> int:
    cfg = RouterConfig.from_env()
    try:
        endpoint = urlsplit(cfg.local_url)
    except ValueError:
        print("Local smoke test requires a valid loopback Ollama endpoint.")
        return 2
    if (endpoint.scheme not in ("http", "https")
            or endpoint.hostname not in ("127.0.0.1", "localhost", "::1")
            or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment
            or endpoint.username or endpoint.password):
        print("Local smoke test requires a loopback Ollama endpoint.")
        return 2
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
            async with client.get(f"{cfg.local_url}/api/tags", allow_redirects=False) as response:
                if response.status != 200:
                    print("Could not list local Ollama models.")
                    return 2
                data = await response.json()
        models = data.get("models")
        if not isinstance(models, list) or cfg.local_model not in {
            item.get("name") for item in models if isinstance(item, dict)
        }:
            print("Configured local model is unavailable. Check ollama list.")
            return 2
        router = ModelRouter(config=replace(cfg, provider="local", policy="local_only"))
        reply = await router.generate(
            [{"role": "user", "content": "Reply with one short greeting."}],
            model=cfg.local_model, provider="local", data_class="private_repo",
            max_tokens=32,
        )
        if not reply.text.strip():
            print("Local model returned an empty response.")
            return 2
        print("Local Ollama model returned a non-empty response.")
        return 0
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
        print("Local Ollama smoke test could not reach or read the server.")
        return 2
    except ModelError as exc:
        print(f"Local Ollama smoke test stopped: {exc.code}.")
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(smoke()))
