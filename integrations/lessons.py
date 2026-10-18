"""Record and retrieve evidence-backed repository lessons."""
from __future__ import annotations
import re
from learning import add_verified_lesson, list_verified_lessons
NAME = "lessons"
DESCRIPTION = "record and retrieve explicitly verified project lessons"
USAGE = ["flanx lessons owner/repo", "flanx lesson owner/repo | failure | root cause | prevention | evidence"]
ADD_RE = re.compile(r"^\s*lesson\s+(.+)$", re.IGNORECASE | re.DOTALL)
LIST_RE = re.compile(r"^\s*lessons\s+([\w.-]+/[\w.-]+)\s*$", re.IGNORECASE)

def match(text: str) -> dict | None:
    raw = (text or "").strip()
    add = ADD_RE.match(raw)
    if add:
        parts = [part.strip() for part in add.group(1).split("|")]
        if len(parts) == 5:
            return {"action": "add", "repository": parts[0], "failure": parts[1], "root_cause": parts[2], "prevention": parts[3], "evidence": parts[4]}
        return {"action": "invalid"}
    listing = LIST_RE.match(raw)
    if listing:
        return {"action": "list", "repository": listing.group(1)}
    return None

async def handle(event, payload: dict) -> str:
    if payload["action"] == "invalid":
        return "Use `flanx lesson owner/repo | failure | root cause | prevention | evidence`. Only record a lesson after verifying the root cause; never include secrets."
    if payload["action"] == "add":
        try:
            lesson = add_verified_lesson(payload["repository"], payload["failure"], payload["root_cause"], payload["prevention"], payload["evidence"])
        except ValueError as exc:
            return f"Couldn't save that lesson: {exc}."
        return f"Saved verified lesson for `{lesson['repository']}`.\n**Prevention:** {lesson['prevention']}\n**Evidence:** {lesson['evidence']}"
    lessons = list_verified_lessons(payload["repository"])
    if not lessons:
        return f"No verified lessons stored for `{payload['repository']}` yet."
    lines = [f"**Verified lessons for `{payload['repository']}`**"]
    for index, lesson in enumerate(lessons[:10], 1):
        lines.append(f"{index}. **Failure:** {lesson['failure']}\n   **Root cause:** {lesson['root_cause']}\n   **Prevention:** {lesson['prevention']}\n   **Evidence:** {lesson['evidence']}")
    if len(lessons) > 10:
        lines.append(f"Showing 10 of {len(lessons)} lessons.")
    return "\n".join(lines)
