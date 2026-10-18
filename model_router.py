"""Task-aware model selection for Project Max.

The router chooses configured FreeLLMAPI model/profile identifiers. It does not
make network calls and never assumes a provider profile exists. All settings are
optional; legacy deployments continue to use AI_MODEL as the fallback.
"""

from __future__ import annotations

import os
from typing import Mapping

TASK_ENV_VARS: Mapping[str, str] = {
    "chat": "MAX_MODEL_CHAT",
    "general": "MAX_MODEL_GENERAL",
    "research": "MAX_MODEL_RESEARCH",
    "coding": "MAX_MODEL_CODING",
    "review": "MAX_MODEL_REVIEW",
}

# These are configuration suggestions, not assumed FreeLLMAPI profile names.
# Set a value only after confirming the profile/model exists in the gateway.
def route_model(
    task: str,
    *,
    fallback: str | None = None,
    override: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the configured model/profile for a task, with safe legacy fallback.

    Precedence: explicit override, task-specific MAX_MODEL_* variable, fallback
    argument, AI_MODEL environment variable, then an empty string. Unknown task
    categories intentionally use the general fallback instead of guessing.
    """
    env = os.environ if environ is None else environ
    if override and override.strip():
        return override.strip()

    task_key = (task or "").strip().lower()
    variable = TASK_ENV_VARS.get(task_key)
    if variable:
        configured = env.get(variable, "").strip()
        if configured:
            return configured

    if fallback and fallback.strip():
        return fallback.strip()

    legacy = env.get("AI_MODEL", "").strip()
    if legacy:
        return legacy

    return ""


def configured_routes(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return effective routes for diagnostics without exposing any secrets."""
    env = os.environ if environ is None else environ
    fallback = env.get("AI_MODEL", "").strip() or "qwen-2.5-coder-32b"
    return {
        task: route_model(task, fallback=fallback, environ=env)
        for task in TASK_ENV_VARS
    }
