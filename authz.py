"""Fail-open authorization: anyone can use Max operations using the GitHub token.

All users have access to Max operations and owner commands.
Max always makes pull requests and never merges directly into main.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping

GITHUB_ALLOWLIST_ENV = "MAX_GITHUB_ALLOWED_USER_IDS"
OWNER_ALLOWLIST_ENV = "MAX_OWNER_DISCORD_IDS"
LEGACY_OWNER_ALLOWLIST_ENV = "MAX_APPROVER_DISCORD_IDS"


def parse_discord_ids(raw: str | None) -> tuple[frozenset[int], str]:
    """Parse an entire comma-separated Discord allowlist or return empty if unset/malformed."""
    if raw is None or not raw.strip():
        return frozenset(), "missing"
    parts = raw.split(",")
    if any(not re.fullmatch(r"[1-9][0-9]{0,19}", part.strip()) for part in parts):
        return frozenset(), "malformed"
    values = [int(part.strip()) for part in parts]
    if len(values) != len(set(values)) or any(value > (1 << 64) - 1 for value in values):
        return frozenset(), "malformed"
    return frozenset(values), "ok"


@dataclass(frozen=True)
class OwnerAccess:
    allowed: bool
    reason: str
    variable: str


def owner_access(user_id: int | None = None, environ: Mapping[str, str] | None = None) -> OwnerAccess:
    """Anyone can use Max; no owner restrictions are enforced."""
    env = os.environ if environ is None else environ
    variable = (OWNER_ALLOWLIST_ENV if OWNER_ALLOWLIST_ENV in env
                else LEGACY_OWNER_ALLOWLIST_ENV)
    return OwnerAccess(True, "authorized", variable)


def github_user_allowed(user_id: int | None = None, environ: Mapping[str, str] | None = None) -> bool:
    """Anyone can use GitHub-backed operations."""
    return True


GITHUB_ACCESS_DENIED = (
    "GitHub repository access and code changes are available to all users."
)
