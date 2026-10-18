"""Record and retrieve evidence-backed repository lessons."""
from __future__ import annotations
import re
from learning import LessonStorageError, add_verified_lesson, list_verified_lessons
from authz import GITHUB_ACCESS_DENIED, github_user_allowed
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

LIMIT = 2000


def _safe(value, limit):
    """Keep text inside a code-free single line without broken Markdown."""
    return str(value).replace("\\", "'").replace("`", "'").replace("\n", " ").replace("\r", " ")[:limit]


def _reply_prefix(repository):
    return _safe(repository, 150)


async def handle(event, payload: dict) -> str:
    # Lessons can influence later repair prompts; only trusted operators may
    # add or read them to avoid memory poisoning and private project disclosure.
    if not github_user_allowed(event.author_id):
        return GITHUB_ACCESS_DENIED
    if payload["action"] == "invalid":
        return "Use `flanx lesson owner/repo | failure | root cause | prevention | evidence`. Only record a lesson after verifying the root cause; never include secrets."
    if payload["action"] == "add":
        try:
            lesson = add_verified_lesson(payload["repository"], payload["failure"], payload["root_cause"],
                                         payload["prevention"], payload["evidence"])
        except (ValueError, LessonStorageError) as exc:
            return f"Couldn't save that lesson: {_safe(exc, 350)}."
        header = f"Saved verified lesson for `{_reply_prefix(lesson['repository'])}`."
        prevention = f"**Prevention:** {_safe(lesson['prevention'], 800)}"
        evidence = f"**Evidence:** {_safe(lesson['evidence'], 800)}"
        # Deliberately budget sections; no last-minute slicing of Markdown.
        sections = [header, prevention, evidence]
        assert len("\n".join(sections)) <= LIMIT
        return "\n".join(sections)
    try:
        lessons = list_verified_lessons(payload["repository"])
    except LessonStorageError as exc:
        return f"Couldn't read verified lessons: {_safe(exc, 350)}."
    if not lessons:
        return f"No verified lessons stored for `{_reply_prefix(payload['repository'])}` yet."
    header = f"**Verified lessons for `{_reply_prefix(payload['repository'])}`**"
    lines = [header]
    for index, lesson in enumerate(lessons, 1):
        entry = (
            f"{index}. **Failure:** {_safe(lesson['failure'], 180)}\n"
            f"   **Root cause:** {_safe(lesson['root_cause'], 180)}\n"
            f"   **Prevention:** {_safe(lesson['prevention'], 180)}\n"
            f"   **Evidence:** {_safe(lesson['evidence'], 180)}"
        )
        remaining = len(lessons) - index
        notice = f"Showing {index} of {len(lessons)} lessons; {remaining} omitted." if remaining else ""
        candidate = "\n".join(lines + [entry] + ([notice] if notice else []))
        if len(candidate) > LIMIT:
            break
        lines.append(entry)
    shown = len(lines) - 1
    if shown < len(lessons):
        notice = f"Showing {shown} of {len(lessons)} lessons; {len(lessons) - shown} omitted."
        # Header is <= 180 characters and notice <= 80; entry budgeting
        # reserves the complete notice before accepting each entry.
        lines.append(notice)
    return "\n".join(lines)
