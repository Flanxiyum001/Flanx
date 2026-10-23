"""Fail-closed authorization: owner and GitHub access require explicit allowlists.

All users have access to Max's public chat operations, but owner commands
(connection management, Composio admin, memory admin, inference diagnostics,
GitHub visibility) and GitHub-backed repository operations require explicit
configuration of trusted Discord user IDs.

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

# Safe diagnostic messages that never reveal configured IDs.
_DENIED_MISSING = "access denied: no trusted operator IDs are configured"
_DENIED_NOT_FOUND = "access denied: your Discord ID is not in the trusted operator allowlist"
_DENIED_MALFORMED = "access denied: the trusted operator allowlist is malformed"
_DENIED_GITHUB_MISSING = "GitHub repository access is not configured for any user"
_DENIED_GITHUB_NOT_FOUND = "GitHub repository access is not configured for your account"
_DENIED_GITHUB_MALFORMED = "GitHub repository access configuration is malformed"


def parse_discord_ids(raw: str | None) -> tuple[frozenset[int], str]:
    """Parse a comma-separated Discord allowlist.

    Returns (frozenset_of_ids, status) where status is one of:
    - "ok": valid, non-empty, unique IDs parsed
    - "missing": raw is None or empty/whitespace only
    - "malformed": raw contains invalid values, duplicates, or out-of-range numbers
    """
    if raw is None or not raw.strip():
        return frozenset(), "missing"
    parts = raw.split(",")
    values: list[int] = []
    seen: set[int] = set()
    for part in parts:
        stripped = part.strip()
        if not stripped:
            return frozenset(), "malformed"
        if not re.fullmatch(r"[1-9][0-9]{0,19}", stripped):
            return frozenset(), "malformed"
        value = int(stripped)
        if value in seen:
            return frozenset(), "malformed"
        seen.add(value)
        values.append(value)
    if not values:
        return frozenset(), "malformed"
    return frozenset(values), "ok"


@dataclass(frozen=True)
class OwnerAccess:
    allowed: bool
    reason: str
    variable: str


def owner_access(user_id: int | None = None, environ: Mapping[str, str] | None = None) -> OwnerAccess:
    """Enforce owner authorization using MAX_OWNER_DISCORD_IDS.

    If MAX_OWNER_DISCORD_IDS is absent, falls back to MAX_APPROVER_DISCORD_IDS
    for legacy compatibility. If the primary variable exists but is empty or
    malformed, fails closed (no fallback). Returns safe diagnostics that do
    not reveal configured IDs.
    """
    env = os.environ if environ is None else environ

    # Determine which variable to use: primary first, legacy fallback only when absent.
    if OWNER_ALLOWLIST_ENV in env:
        variable = OWNER_ALLOWLIST_ENV
        raw = env[OWNER_ALLOWLIST_ENV]
    elif LEGACY_OWNER_ALLOWLIST_ENV in env:
        variable = LEGACY_OWNER_ALLOWLIST_ENV
        raw = env[LEGACY_OWNER_ALLOWLIST_ENV]
    else:
        # Neither variable is set: fail closed.
        return OwnerAccess(False, _DENIED_MISSING, variable="")

    ids, status = parse_discord_ids(raw)
    if status == "missing":
        # Variable exists but is empty: fail closed, no fallback.
        return OwnerAccess(False, _DENIED_MALFORMED, variable)
    if status == "malformed":
        return OwnerAccess(False, _DENIED_MALFORMED, variable)
    if user_id is None:
        return OwnerAccess(False, _DENIED_MISSING, variable)
    if user_id in ids:
        return OwnerAccess(True, "authorized", variable)
    return OwnerAccess(False, _DENIED_NOT_FOUND, variable)


def github_user_allowed(user_id: int | None = None, environ: Mapping[str, str] | None = None) -> bool:
    """Enforce GitHub-backed operation authorization using MAX_GITHUB_ALLOWED_USER_IDS.

    Returns True only when the variable is present, well-formed, and contains
    the requester's Discord ID. Missing, empty, malformed, or non-matching
    configurations all fail closed.
    """
    env = os.environ if environ is None else environ
    raw = env.get(GITHUB_ALLOWLIST_ENV)
    ids, status = parse_discord_ids(raw)
    if status != "ok":
        return False
    if user_id is None:
        return False
    return user_id in ids


GITHUB_ACCESS_DENIED = (
    "GitHub repository access and code changes require explicit authorization. "
    "Ask the bot owner to configure MAX_GITHUB_ALLOWED_USER_IDS."
)
