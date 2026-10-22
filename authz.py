"""Fail-closed authorization for Discord operations using the GitHub token.

A bot joined to multiple channels must not share repository write permissions
with every Discord user. This list is independent of the bot's human-readable
name and cannot be supplied by an untrusted Discord message.
"""
from __future__ import annotations

import os
from typing import Mapping

GITHUB_ALLOWLIST_ENV = "MAX_GITHUB_ALLOWED_USER_IDS"


def github_user_allowed(user_id: int, environ: Mapping[str, str] | None = None) -> bool:
    """Only explicitly listed Discord IDs can use GitHub-backed operations."""
    env = os.environ if environ is None else environ
    text = env.get(GITHUB_ALLOWLIST_ENV, "")
    try:
        user = int(user_id)
    except (TypeError, ValueError):
        return False
    if user <= 0:
        return False
    for field in text.split(","):
        candidate = field.strip()
        if candidate.isdecimal() and int(candidate) == user:
            return True
    return False


GITHUB_ACCESS_DENIED = (
    "GitHub repository access and code changes are restricted to authorized "
    "operators. The owner must set MAX_GITHUB_ALLOWED_USER_IDS in the bot "
    "environment using trusted Discord user IDs. No GitHub request was made."
)
