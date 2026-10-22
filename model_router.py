"""Model selection, privacy policy, and provider response normalization for Max."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Mapping
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp

TASK_ENV_VARS: Mapping[str, str] = {
    "chat": "MAX_MODEL_CHAT", "general": "MAX_MODEL_GENERAL",
    "research": "MAX_MODEL_RESEARCH", "coding": "MAX_MODEL_CODING",
    "review": "MAX_MODEL_REVIEW",
}
ROLE_TASKS = {"planner": "general", "coder": "coding", "reviewer": "review", "tester": "review"}
PRIVATE_DATA = frozenset({"private_repo", "source", "memory"})
LOG = logging.getLogger(__name__)
DEFAULT_LOCAL_MODEL = "qwen3:8b"
DEFAULT_VISION_MODEL = "qwen3-vl:8b"
_LABELED_SECRET = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|"
    r"authorization|cookie|client[_-]?secret)\s*[:=]\s*[^\r\n,}]+")
_TOKEN_VALUE = re.compile(r"(?i)\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)


def _redact_cloud_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    redacted = []
    for message in messages:
        content = _PRIVATE_KEY.sub("[redacted private key]", message["content"])
        content = _LABELED_SECRET.sub("[redacted credential]", content)
        content = _TOKEN_VALUE.sub("[redacted token]", content)
        redacted.append({"role": message["role"], "content": content})
    return redacted


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _enabled(env: Mapping[str, str], key: str) -> bool:
    return env.get(key, "").lower() in ("1", "true", "yes")


def route_model(task: str, *, fallback: str | None = None,
                override: str | None = None,
                environ: Mapping[str, str] | None = None) -> str:
    """Preserve original override, task, fallback, AI_MODEL precedence."""
    env = os.environ if environ is None else environ
    if override and override.strip():
        return override.strip()
    variable = TASK_ENV_VARS.get((task or "").strip().lower())
    if variable and env.get(variable, "").strip():
        return env[variable].strip()
    if fallback and fallback.strip():
        return fallback.strip()
    return env.get("AI_MODEL", "").strip()


def configured_routes(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Legacy task routes, with no credentials."""
    env = os.environ if environ is None else environ
    fallback = env.get("AI_MODEL", "").strip() or DEFAULT_LOCAL_MODEL
    return {task: route_model(task, fallback=fallback, environ=env) for task in TASK_ENV_VARS}


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        return max(1, int(env.get(key, str(default))))
    except ValueError:
        return default


@dataclass(frozen=True)
class RouterConfig:
    provider: str
    policy: str
    allow_cloud_private: bool
    allowed_private_classes: frozenset[str]
    local_url: str
    cloud_url: str
    cloud_key: str = field(repr=False)
    local_model: str
    vision_model: str
    local_concurrency: int
    cloud_concurrency: int
    timeout_seconds: int
    context_tokens: int
    input_chars: int
    keep_alive: str
    auto_cloud_fallback: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RouterConfig:
        env = os.environ if environ is None else environ
        return cls(
            provider=env.get("MAX_INFERENCE_PROVIDER", "local").strip().lower() or "local",
            policy=env.get("MAX_INFERENCE_POLICY", "local_only").strip().lower() or "local_only",
            allow_cloud_private=_enabled(env, "MAX_ALLOW_CLOUD_PRIVATE_CONTEXT"),
            allowed_private_classes=frozenset(
                name for name, variable in (
                    ("private_repo", "MAX_ALLOW_CLOUD_PRIVATE_REPO"),
                    ("source", "MAX_ALLOW_CLOUD_SOURCE"),
                    ("memory", "MAX_ALLOW_CLOUD_MEMORY"),
                ) if _enabled(env, variable)
            ),
            local_url=(env.get("MAX_OLLAMA_BASE_URL") or env.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/"),
            cloud_url=env.get("AI_BASE_URL", "").rstrip("/"),
            cloud_key=env.get("AI_API_KEY", ""),
            local_model=env.get("MAX_LOCAL_MODEL", DEFAULT_LOCAL_MODEL).strip() or DEFAULT_LOCAL_MODEL,
            vision_model=env.get("MAX_VISION_MODEL", DEFAULT_VISION_MODEL).strip() or DEFAULT_VISION_MODEL,
            local_concurrency=_positive_int(env, "MAX_LOCAL_INFERENCE_CONCURRENCY", 1),
            cloud_concurrency=_positive_int(env, "MAX_CLOUD_INFERENCE_CONCURRENCY", 2),
            timeout_seconds=_positive_int(env, "MAX_MODEL_TIMEOUT_SECONDS", 110),
            context_tokens=_positive_int(env, "MAX_MODEL_CONTEXT_TOKENS", 8192),
            input_chars=_positive_int(env, "MAX_MODEL_INPUT_CHARS", 50000),
            keep_alive=env.get("MAX_OLLAMA_KEEP_ALIVE", "0"),
            auto_cloud_fallback=_enabled(env, "MAX_AUTO_CLOUD_FALLBACK"),
        )


class ModelError(Exception):
    """Safe failure: no prompt, response body, or credential in its message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ModelReply:
    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    provider: str = "unknown"


class ModelRouter:
    def __init__(self, config: RouterConfig | None = None, *, local_client=None,
                 cloud_client=None, environ: Mapping[str, str] | None = None):
        self.env = dict(os.environ if environ is None else environ)
        self.config = config or RouterConfig.from_env(self.env)
        self.local_client = local_client
        self.cloud_client = cloud_client
        self._local_slots = asyncio.Semaphore(self.config.local_concurrency)
        self._cloud_slots = asyncio.Semaphore(self.config.cloud_concurrency)

    def selected_model(self, task: str = "general", *, role: str | None = None,
                       override: str | None = None, provider: str = "local") -> str:
        if role:
            if role not in ROLE_TASKS:
                raise ModelError("configuration", "Unknown specialist role.")
            specific = self.env.get(f"MAX_MODEL_{role.upper()}", "").strip()
            if specific and not override:
                return specific
            task = ROLE_TASKS[role]
        if task == "vision":
            return override or self.env.get("MAX_MODEL_VISION", "").strip() or self.config.vision_model
        if provider in {"freellm", "cloud"}:
            fallback = self.env.get("AI_MODEL", "").strip()
        elif self.config.provider == "local":
            fallback = self.env.get("AI_MODEL", "").strip() or self.config.local_model
        else:
            fallback = self.config.local_model
        return route_model(task, override=override,
                           fallback=fallback,
                           environ=self.env)

    def route_description(self) -> str:
        """Safe provider selection label for task metadata and diagnostics."""
        target = self.config.provider
        if target == "auto":
            return "auto (Ollama first; FreeLLM fallback configured)" if self.config.auto_cloud_fallback else "auto (Ollama only)"
        return "FreeLLM" if target in {"freellm", "cloud"} else "Ollama"

    def _cloud_permission(self, classes: tuple[str, ...]) -> None:
        cfg = self.config
        if cfg.policy != "cloud_allowed":
            raise ModelError("provider_disabled", "FreeLLM is disabled by MAX_INFERENCE_POLICY; set cloud_allowed to enable it.")
        if any(item in PRIVATE_DATA and not cfg.allow_cloud_private
               and item not in cfg.allowed_private_classes for item in classes):
            raise ModelError("privacy_blocked", "Privacy policy blocks FreeLLM for this data class. Use Ollama or explicitly approve this class.")

    def _cloud_configuration(self) -> None:
        cfg = self.config
        if not cfg.cloud_key and self.cloud_client is None:
            raise ModelError("missing_credentials", "FreeLLM requires AI_API_KEY.")
        try:
            endpoint = urlsplit(cfg.cloud_url)
            valid = endpoint.scheme in {"http", "https"} and bool(endpoint.hostname) and not (
                endpoint.username or endpoint.password or endpoint.query or endpoint.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise ModelError("configuration", "FreeLLM requires AI_BASE_URL set to its HTTP(S) OpenAI-compatible API base URL without embedded credentials.")

    async def generate(self, messages: list[dict[str, str]], *, task: str = "general",
                       role: str | None = None, model: str | None = None,
                       provider: str | None = None,
                       data_class: str | tuple[str, ...] = "private_repo",
                       temperature: float | None = None,
                       max_tokens: int | None = None) -> ModelReply:
        cfg = self.config
        target = (provider or cfg.provider).strip().lower()
        if target == "cloud":
            target = "freellm"  # compatibility with the existing configuration
        if target not in ("local", "freellm", "auto") or cfg.policy not in ("local_only", "cloud_allowed"):
            raise ModelError("configuration", "Inference provider or privacy policy is invalid.")
        classes = (data_class,) if isinstance(data_class, str) else data_class
        if not isinstance(classes, tuple) or not classes or any(
            item not in ("general", "public", "private_repo", "source", "memory")
            for item in classes
        ):
            raise ModelError("configuration", "Unknown inference data class.")
        if target in {"local", "auto"}:
            try:
                endpoint = urlsplit(cfg.local_url)
                is_local = (
                    endpoint.scheme in ("http", "https")
                    and endpoint.hostname in ("127.0.0.1", "localhost", "::1")
                    and not endpoint.username and not endpoint.password
                    and endpoint.path in ("", "/") and not endpoint.query
                    and not endpoint.fragment
                )
            except ValueError:
                is_local = False
            if not is_local:
                raise ModelError("privacy_blocked", "Local-only Ollama inference requires a loopback endpoint. Configure a local Ollama server.")
        if not isinstance(messages, list) or any(
            not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant")
            or not isinstance(m.get("content"), str) for m in messages
        ):
            raise ModelError("configuration", "Invalid model messages.")
        if sum(len(m["content"]) for m in messages) > cfg.input_chars:
            raise ModelError("context_limit", "The request exceeds MAX_MODEL_INPUT_CHARS. Shorten its context.")
        if target == "freellm":
            self._cloud_permission(classes)
            self._cloud_configuration()
            chosen = self.selected_model(task, role=role, override=model, provider="freellm")
            if not chosen:
                raise ModelError("configuration", "FreeLLM requires AI_MODEL or a task-specific MAX_MODEL_* identifier.")
            LOG.info("model route provider=freellm task=%s data_classes=%s",
                     task if task in TASK_ENV_VARS else "other", ",".join(classes))
            async with self._cloud_slots:
                return await self._cloud(_redact_cloud_messages(messages), chosen, temperature, max_tokens)
        local_model = self.selected_model(task, role=role, override=model, provider="local")
        LOG.info("model route provider=ollama task=%s data_classes=%s",
                 task if task in TASK_ENV_VARS else "other", ",".join(classes))
        try:
            async with self._local_slots:
                return await self._local(messages, local_model, temperature, max_tokens)
        except ModelError as local_error:
            if target != "auto" or not cfg.auto_cloud_fallback or local_error.code not in {
                "missing_model", "unavailable", "timeout", "malformed_response"
            }:
                raise
            try:
                self._cloud_permission(classes)
            except ModelError as blocked:
                raise ModelError(local_error.code, f"{local_error} FreeLLM fallback blocked: {blocked}") from None
            try:
                self._cloud_configuration()
                cloud_model = self.selected_model(task, role=role, override=model, provider="freellm")
                if not cloud_model:
                    raise ModelError("configuration", "FreeLLM requires AI_MODEL or a task-specific MAX_MODEL_* identifier.")
                LOG.info("model route provider=freellm fallback_from=ollama task=%s data_classes=%s",
                         task if task in TASK_ENV_VARS else "other", ",".join(classes))
                async with self._cloud_slots:
                    return await self._cloud(_redact_cloud_messages(messages), cloud_model, temperature, max_tokens)
            except ModelError as cloud_error:
                raise ModelError("providers_failed", f"Ollama: {local_error} FreeLLM: {cloud_error}") from None

    async def _local(self, messages, model, temperature, max_tokens) -> ModelReply:
        cfg = self.config
        payload = {"model": model, "messages": messages, "stream": False, "think": False,
                   "keep_alive": cfg.keep_alive, "options": {"num_ctx": cfg.context_tokens}}
        if temperature is not None:
            payload["options"]["temperature"] = temperature
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens
        try:
            if self.local_client is None:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=cfg.timeout_seconds)) as client:
                    async with client.post(f"{cfg.local_url}/api/chat", json=payload,
                                           allow_redirects=False) as raw:
                        response = SimpleNamespace(status_code=raw.status, text=await raw.text())
                        response.json = lambda: json.loads(response.text)
            else:
                response = await asyncio.wait_for(
                    self.local_client.post(f"{cfg.local_url}/api/chat", json=payload), cfg.timeout_seconds)
            if response.status_code == 404:
                raise ModelError("missing_model", "The selected local model is unavailable. Check ollama list and the configured model ID.")
            if response.status_code >= 300:
                body = response.text.lower()  # classify only; never return or log it
                if response.status_code == 400 and ("context" in body or "token" in body):
                    raise ModelError("context_limit", "The local model rejected the request context.")
                raise ModelError("unavailable", "The local model server rejected the request.")
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
                raise ValueError("invalid response")
            content = data["message"].get("content")
            if not isinstance(content, str) or not content.strip() or data.get("done") is not True:
                raise ValueError("invalid response")
            return ModelReply(content.strip(), str(data.get("model") or model),
                              _count(data.get("prompt_eval_count")), _count(data.get("eval_count")), "ollama")
        except ModelError:
            raise
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
            raise ModelError("timeout", "Local model request timed out; the server may still be finishing it.") from None
        except (aiohttp.ClientError, OSError):
            raise ModelError("unavailable", "The local model server is unavailable. Check Ollama.") from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ModelError("malformed_response", "The local model server returned an invalid response.") from None

    async def _cloud(self, messages, model, temperature, max_tokens) -> ModelReply:
        cfg = self.config
        owned_client = None
        try:
            client = self.cloud_client
            if client is None:
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=cfg.cloud_key, base_url=cfg.cloud_url, timeout=cfg.timeout_seconds)
                owned_client = client
            args = {"model": model, "messages": messages}
            if temperature is not None:
                args["temperature"] = temperature
            if max_tokens is not None:
                args["max_tokens"] = max_tokens
            response = await asyncio.wait_for(client.chat.completions.create(**args), cfg.timeout_seconds)
            content = response.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise ValueError("invalid response")
            usage = getattr(response, "usage", None)
            return ModelReply(content.strip(), getattr(response, "model", None) or model,
                              _count(getattr(usage, "prompt_tokens", None)),
                              _count(getattr(usage, "completion_tokens", None)), "freellm")
        except asyncio.TimeoutError:
            raise ModelError("timeout", "FreeLLM request timed out; the provider may still be finishing it.") from None
        except (ValueError, IndexError, TypeError, AttributeError):
            raise ModelError("malformed_response", "FreeLLM returned an invalid chat completion.") from None
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status == 429:
                raise ModelError("rate_limited", "FreeLLM rate limit reached. Retry later or select Ollama.") from None
            if status in (401, 403):
                raise ModelError("authentication", "FreeLLM rejected AI_API_KEY or access to the configured model.") from None
            if status in (400, 404, 405, 422):
                raise ModelError("rejected", f"FreeLLM rejected the request (HTTP {status}); check AI_BASE_URL and AI_MODEL.") from None
            if "timeout" in type(exc).__name__.lower():
                raise ModelError("timeout", "FreeLLM request timed out.") from None
            raise ModelError("unavailable", "FreeLLM connection failed or its endpoint is unavailable; check AI_BASE_URL.") from None
        finally:
            if owned_client is not None:
                try:
                    await owned_client.close()
                except Exception:
                    pass
