"""Evidence-backed, repository-scoped lessons for Project Max."""
from __future__ import annotations
import json, os, re, threading
from datetime import datetime, timezone
LESSONS_FILE = os.getenv("MAX_LESSONS_FILE", "max_lessons.json")
MAX_LESSONS = 500
MAX_FIELD_CHARS = 800
_lock = threading.Lock()

def _load() -> list[dict]:
    try:
        with open(LESSONS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("lessons"), list):
            return [item for item in data["lessons"] if isinstance(item, dict)]
    except (OSError, ValueError, TypeError):
        pass
    return []

def _save(lessons: list[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(LESSONS_FILE)), exist_ok=True)
    temporary = f"{LESSONS_FILE}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"lessons": lessons}, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, LESSONS_FILE)

def add_verified_lesson(repository: str, failure: str, root_cause: str, prevention: str, evidence: str) -> dict:
    """Save a verified lesson only when its evidence and prevention are explicit."""
    fields = {"repository": repository, "failure": failure, "root_cause": root_cause, "prevention": prevention, "evidence": evidence}
    fields = {key: str(value).strip()[:MAX_FIELD_CHARS] for key, value in fields.items()}
    if not all(fields.values()):
        raise ValueError("repository, failure, root cause, prevention, and evidence are required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", fields["repository"]):
        raise ValueError("repository must use exactly owner/repo format")
    with _lock:
        lessons = _load()
        signature = tuple(fields[k].casefold() for k in ("repository", "failure", "root_cause"))
        for lesson in lessons:
            existing = tuple(str(lesson.get(k, "")).casefold() for k in ("repository", "failure", "root_cause"))
            if existing == signature:
                lesson.update(fields)
                lesson["updated_at"] = datetime.now(timezone.utc).isoformat()
                lesson["status"] = "verified"
                _save(lessons)
                return dict(lesson)
        now = datetime.now(timezone.utc).isoformat()
        lesson = {"id": f"lesson-{int(datetime.now(timezone.utc).timestamp() * 1000000)}", **fields, "status": "verified", "created_at": now, "updated_at": now}
        lessons.append(lesson)
        _save(lessons[-MAX_LESSONS:])
        return dict(lesson)

def list_verified_lessons(repository: str) -> list[dict]:
    key = (repository or "").strip().casefold()
    if not key:
        return []
    with _lock:
        lessons = _load()
    matches = [item for item in lessons if str(item.get("repository", "")).casefold() == key and item.get("status") == "verified"]
    return sorted(matches, key=lambda item: str(item.get("updated_at", "")), reverse=True)
