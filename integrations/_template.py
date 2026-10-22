"""TEMPLATE — copy this file to a new name (no leading underscore) to add an integration.

Files starting with ``_`` are ignored by discovery, so this template stays dormant
until you rename it. Steps:

1. Copy ``integrations/_template.py`` to ``integrations/<name>.py``.
2. Fill in ``NAME``, ``DESCRIPTION``, ``USAGE``.
3. Implement ``match()``: return a payload dict to claim a chat message, else None.
4. Implement ``handle()``: do the work (HTTP call, file, GitHub, …) and return the
   reply string Max should post. Use ``asyncio.to_thread`` inside for blocking I/O.
5. Restart the bot — ``max integrations`` now lists it. No changes to ``main.py``.

Contract (enforced by ``integrations/__init__.py``): a module missing NAME, match or
handle is skipped with a console warning instead of crashing the bot.
"""

from __future__ import annotations

import re

NAME = "template"
DESCRIPTION = "one-line summary shown by `max integrations`"
USAGE = ["max template hello"]

PATTERN = re.compile(r"^\s*template\s+(?P<what>.+)$", re.IGNORECASE)


def match(text: str) -> dict | None:
    """Claim a message by returning a payload dict, or return None to pass."""
    m = PATTERN.match(text or "")
    if m:
        return {"what": m.group("what")}
    return None


async def handle(event, payload: dict) -> str:
    """Do the work and return the reply Max should post (or None for silence)."""
    # Blocking work? wrap it:  result = await asyncio.to_thread(some_http_call, ...)
    return f"Template integration says: {payload['what']}"
