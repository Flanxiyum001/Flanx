"""Evidence-backed, repository-scoped lessons stored transactionally in SQLite."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

LESSONS_FILE = os.getenv("MAX_LESSONS_FILE", "max_lessons.json")
LESSONS_DB = os.getenv("MAX_LESSONS_DB", "max_lessons.sqlite3")
MAX_LESSONS = 500
MAX_FIELD_CHARS = 800
_KEYS = ("repository", "failure", "root_cause", "prevention", "evidence")


class LessonStorageError(RuntimeError):
    """Lesson storage could not be read or written safely."""


def _validate(values):
    if not isinstance(values, dict) or any(not isinstance(values.get(k), str) for k in _KEYS):
        raise ValueError("lesson must contain five string fields")
    fields = {key: values[key].strip()[:MAX_FIELD_CHARS] for key in _KEYS}
    if not all(fields.values()):
        raise ValueError("repository, failure, root cause, prevention, and evidence are required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", fields["repository"]):
        raise ValueError("repository must use exactly owner/repo format")
    return fields


def _connect():
    try:
        parent = os.path.dirname(os.path.abspath(LESSONS_DB))
        os.makedirs(parent, exist_ok=True)
        connection = sqlite3.connect(LESSONS_DB, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS lessons (
                id TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                failure TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                prevention TEXT NOT NULL,
                evidence TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(repository COLLATE NOCASE, failure COLLATE NOCASE, root_cause COLLATE NOCASE)
            )
        """)
        connection.commit()
        return connection
    except (sqlite3.Error, OSError) as exc:
        raise LessonStorageError(f"Cannot open lesson database {LESSONS_DB!r}: {exc}") from exc


def _upsert(connection, fields, *, record=None):
    now = datetime.now(timezone.utc).isoformat()
    existing = connection.execute(
        "SELECT * FROM lessons WHERE repository = ? COLLATE NOCASE AND failure = ? COLLATE NOCASE AND root_cause = ? COLLATE NOCASE",
        (fields["repository"], fields["failure"], fields["root_cause"]),
    ).fetchone()
    if existing:
        connection.execute(
            """UPDATE lessons SET repository=?, failure=?, root_cause=?, prevention=?, evidence=?,
               status='verified', updated_at=? WHERE id=?""",
            (*[fields[k] for k in _KEYS], (record or {}).get("updated_at", now), existing["id"]),
        )
        identifier = existing["id"]
    else:
        import uuid
        identifier = (record or {}).get("id") or f"lesson-{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO lessons (id, repository, failure, root_cause, prevention, evidence, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (identifier, *[fields[k] for k in _KEYS], "verified",
             (record or {}).get("created_at", now), (record or {}).get("updated_at", now)),
        )
    connection.execute(
        "DELETE FROM lessons WHERE id IN (SELECT id FROM lessons ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
        (MAX_LESSONS,),
    )
    row = connection.execute("SELECT * FROM lessons WHERE id=?", (identifier,)).fetchone()
    return dict(row) if row else None


def add_verified_lesson(repository, failure, root_cause, prevention, evidence):
    fields = _validate(dict(zip(_KEYS, (repository, failure, root_cause, prevention, evidence))))
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = _upsert(connection, fields)
        connection.commit()
        return result
    except sqlite3.Error as exc:
        connection.rollback()
        raise LessonStorageError(f"Could not save verified lesson in {LESSONS_DB!r}: {exc}") from exc
    finally:
        connection.close()


def list_verified_lessons(repository):
    key = (repository or "").strip()
    if not key:
        return []
    connection = _connect()
    try:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM lessons WHERE repository = ? COLLATE NOCASE AND status='verified' ORDER BY updated_at DESC",
            (key,),
        ).fetchall()]
    except sqlite3.Error as exc:
        raise LessonStorageError(f"Could not read verified lessons from {LESSONS_DB!r}: {exc}") from exc
    finally:
        connection.close()


def migrate_json(source=None):
    """Explicitly import a validated legacy JSON file; never modify the source."""
    source = source or LESSONS_FILE
    try:
        with open(source, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"Cannot read migration source {source!r}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("lessons"), list):
        raise ValueError("Migration source must be an object containing a lessons list")
    records = data["lessons"]
    validated = []
    for index, record in enumerate(records):
        try:
            fields = _validate(record)
            if record.get("status") != "verified":
                raise ValueError("status must be verified")
            for name in ("id", "created_at", "updated_at"):
                if not isinstance(record.get(name), str) or not record[name]:
                    raise ValueError(f"{name} must be a nonempty string")
            validated.append((fields, record))
        except ValueError as exc:
            raise ValueError(f"Invalid migration record {index}: {exc}") from exc
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for fields, record in validated:
            _upsert(connection, fields, record=record)
        connection.commit()
        return len(validated)
    except sqlite3.Error as exc:
        connection.rollback()
        raise LessonStorageError(f"Migration failed; database transaction rolled back: {exc}") from exc
    finally:
        connection.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Explicit JSON to SQLite verified-lesson migration")
    parser.add_argument("--migrate-json", metavar="PATH", required=True)
    args = parser.parse_args()
    print(f"Imported or updated {migrate_json(args.migrate_json)} source records into {LESSONS_DB}")
