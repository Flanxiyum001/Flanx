"""Clock integration: per-user timezone and current time."""

from __future__ import annotations

import re

from prefs import current_time, is_valid_timezone, set_pref

NAME = "clock"
DESCRIPTION = "per-user timezone and current time"
USAGE = ["max set timezone Asia/Kolkata", "max what time is it", "max time in UTC"]

TIMEZONE_SET_RE = re.compile(
    r"^\s*(?:please\s+)?set\s+(?:your\s+)?timezone\s+(?:to|as|:)?\s*([A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)?)",
    re.IGNORECASE,
)
TIME_NOW_RE = re.compile(
    r"^\s*(?:what(?:'s|\s+is)?\s+(?:the\s+)?(?:current\s+)?time(?:\s+is\s+it)?|time)"
    r"\s*(?:in\s+([A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)?))?\s*$",
    re.IGNORECASE,
)


def match(text: str) -> dict | None:
    m = TIMEZONE_SET_RE.match(text or "")
    if m:
        return {"action": "timezone_set", "tz": m.group(1)}
    m = TIME_NOW_RE.match(text or "")
    if m:
        return {"action": "time", "tz": m.group(1)}
    return None


async def handle(event, payload: dict) -> str:
    if payload["action"] == "timezone_set":
        tz = payload["tz"]
        if not is_valid_timezone(tz):
            return (
                f"Hmm, `{tz}` isn't a timezone I know — they look like "
                "`Asia/Kolkata` or `America/New_York`. Mind checking the spelling?"
            )
        set_pref(event.author_id, "timezone", tz)
        return (
            f"🗺️ Timezone saved — just ask `max what time is it` anytime and "
            f"I'll answer in `{tz}`."
        )

    result = current_time(event.author_id, payload.get("tz"))
    if result is None:
        return (
            "⏰ I don't have a timezone for you yet — say "
            "`max set timezone Asia/Kolkata` once and I'll remember it."
        )
    now, tz = result
    return f"🕒 It's **{now}** in `{tz}`."
