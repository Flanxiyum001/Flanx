"""Notes integration: per-user personal notes remembered across messages."""

from __future__ import annotations

import re

from prefs import add_note, list_notes

NAME = "notes"
DESCRIPTION = "personal notes, remembered per Discord user"
USAGE = ["max remember the beta ships friday", "max show notes"]

NOTE_RE = re.compile(r"^\s*(?:please\s+)?(?:remember|note)\s+(?:that\s+)?(.+)$", re.IGNORECASE | re.DOTALL)
NOTES_LIST_RE = re.compile(
    r"^\s*(?:show\s+|list\s+|my\s+)?notes(?:\s+list)?\s*$|^\s*what\s+did\s+you\s+(?:note|write)\s*$",
    re.IGNORECASE,
)


def match(text: str) -> dict | None:
    m = NOTE_RE.match(text or "")
    if m:
        return {"action": "add", "text": m.group(1).strip()}
    if NOTES_LIST_RE.match(text or ""):
        return {"action": "list"}
    return None


async def handle(event, payload: dict) -> str:
    if payload["action"] == "add":
        count = add_note(event.author_id, payload["text"])
        plural = "s" if count != 1 else ""
        clipped = payload["text"][:150]
        return (
            f"✍️ Noted ({count} note{plural} so far): “{clipped}”\n"
            "Say `max show notes` whenever you want them back."
        )

    notes = list_notes(event.author_id)
    if not notes:
        return "📒 No notes yet — say `max remember <anything>` and I'll keep it for you."
    items = "\n".join(
        f"{i}. {note[:120]}" for i, note in enumerate(notes[-15:], 1)
    )
    more = f"\n…and {len(notes) - 15} more" if len(notes) > 15 else ""
    return f"📒 **Your notes ({len(notes)}):**\n{items}{more}"
