"""Safe, predictable errors for the OpenAI-compatible FreeLLMAPI router.

Do not render exception strings or HTTP response bodies to Discord or logs:
SDK exceptions may contain prompts, authentication headers, and upstream URLs.
"""
from __future__ import annotations

STALE_MODEL_IDS = {"qwen-2.5-coder-32b"}


def configured_model_issue(model: str) -> str | None:
    if not (model or "").strip():
        return "No AI model is configured; set AI_MODEL or a matching MAX_MODEL_* variable."
    if model.strip().lower() in STALE_MODEL_IDS:
        return (
            "The configured model ID is from an obsolete catalog. "
            "Choose a model reported by your FreeLLMAPI /v1/models endpoint "
            "or use its supported auto route."
        )
    return None


def safe_provider_error(exc: Exception) -> str:
    """Describe the failure without echoing potentially sensitive details."""
    status = getattr(exc, "status_code", None)
    if status in {401, 403}:
        return f"AI gateway rejected authentication or access (HTTP {status}). Check its unified API key and permissions."
    if status == 400:
        return "AI gateway rejected the request (HTTP 400). Check the selected model/profile and request format."
    if status == 404:
        return "AI gateway endpoint or selected model was not found (HTTP 404). Verify the /v1 base URL and model catalog."
    if status == 405:
        return "AI gateway rejected the HTTP method (HTTP 405). Verify the OpenAI-compatible /v1 endpoint."
    if status == 429:
        return "AI gateway is rate-limited or out of available provider quota (HTTP 429). Try again later."
    if isinstance(status, int) and status >= 500:
        return f"AI gateway is temporarily unavailable (HTTP {status}). Try again later."
    name = type(exc).__name__
    if name in {"APITimeoutError", "TimeoutError"}:
        return "AI gateway timed out; no successful response was confirmed."
    if name in {"APIConnectionError", "ConnectError", "ConnectionError"}:
        return "Could not connect to the configured AI gateway."
    return "AI request failed. Check gateway availability and Railway logs (without sharing credentials)."
