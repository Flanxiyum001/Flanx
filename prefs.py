"""Per-user companion preferences and notes for Max (persisted JSON).

Holds the personal-assistant side of Max: each Discord user's timezone and
their saved notes. State lives in ``flanx_prefs.json`` (git-ignored) with atomic
writes and a lock, mirroring ``usage.py``. On ephemeral hosts point
``FLANX_PREFS_FILE`` at a persistent path to keep preferences across redeploys.
"""

from __future__ import annotations

import json
import os
import threading
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PREFS_FILE = os.getenv("FLANX_PREFS_FILE", "flanx_prefs.json")

MAX_NOTES_PER_USER = 50
MAX_NOTE_CHARS = 300

_lock = threading.Lock()


class PreferenceStorageError(RuntimeError):
    """Raised when existing preference data cannot be read safely."""


def _empty() -> dict:
    return {"users": {}}


def _load() -> dict:
    try:
        with open(PREFS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _empty()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreferenceStorageError(
            f"Could not read preference storage at {PREFS_FILE!r}; refusing to reset it."
        ) from exc

    if not isinstance(data, dict) or set(data) != {"users"} or not isinstance(data.get("users"), dict):
        raise PreferenceStorageError(
            f"Preference storage at {PREFS_FILE!r} has an invalid structure; refusing to reset it."
        )
    for user in data["users"].values():
        if (not isinstance(user, dict)
                or not isinstance(user.get("prefs"), dict)
                or not isinstance(user.get("notes"), list)
                or any(not isinstance(note, str) for note in user["notes"])):
            raise PreferenceStorageError(
                "Existing preference records are invalid; refusing to reset or overwrite."
            )
    return data


def _save(data: dict) -> None:
    temporary = None
    try:
        parent = os.path.dirname(os.path.abspath(PREFS_FILE))
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=parent, prefix=".flanx-prefs-", delete=False
        ) as handle:
            temporary = handle.name
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, PREFS_FILE)
        temporary = None
    except (OSError, UnicodeError) as exc:
        raise PreferenceStorageError("Could not save preference storage.") from exc
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                # Preserve the original error; a stale temp file can be inspected separately.
                pass


def _user(data: dict, user_id: int) -> dict:
    return data["users"].setdefault(str(user_id), {"prefs": {}, "notes": []})


def set_pref(user_id: int, key: str, value) -> dict:
    with _lock:
        data = _load()
        _user(data, user_id)["prefs"][key] = value
        _save(data)
        return data


def get_pref(user_id: int, key: str, default=None):
    with _lock:
        data = _load()
        return data["users"].get(str(user_id), {}).get("prefs", {}).get(key, default)


def is_valid_timezone(name: str) -> bool:
    """True for any IANA name the system knows ("Asia/Kolkata", "UTC", "GMT", …)."""
    if not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return False


def current_time(user_id: int, requested_tz: str | None = None) -> tuple[str, str] | None:
    """(formatted time, timezone name) for this user — or None if no timezone is known."""
    tz_name = requested_tz or get_pref(user_id, "timezone")
    if not tz_name:
        return None
    if not is_valid_timezone(tz_name):
        return None
    now = datetime.now(ZoneInfo(tz_name))
    return now.strftime("%a %d %b %Y, %I:%M %p").lstrip("0"), tz_name


def add_note(user_id: int, text: str) -> int:
    """Append a personal note; returns the number of notes after the write."""
    note = text.strip()[:MAX_NOTE_CHARS]
    with _lock:
        data = _load()
        user = _user(data, user_id)
        user["notes"].append(note)
        if len(user["notes"]) > MAX_NOTES_PER_USER:
            del user["notes"][: len(user["notes"]) - MAX_NOTES_PER_USER]
        _save(data)
        return len(user["notes"])


def list_notes(user_id: int) -> list[str]:
    with _lock:
        data = _load()
        return list(data["users"].get(str(user_id), {}).get("notes", []))
