"""Evidence-backed repository lessons with explicit, transactional SQLite migration."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone

LESSONS_FILE = os.getenv("MAX_LESSONS_FILE", "max_lessons.json")
LESSONS_DB = os.getenv("MAX_LESSONS_DB", "max_lessons.sqlite3")
MAX_LESSONS = 500
MAX_FIELD_CHARS = 800
_KEYS = ("repository", "failure", "root_cause", "prevention", "evidence")
_COLUMNS = "id, repository, failure, root_cause, prevention, evidence, status, created_at, updated_at"
_SCHEMA = """
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
"""


class LessonStorageError(RuntimeError):
    """A database operation failed; no success should be reported."""


def _validate(values):
    if not isinstance(values, dict) or any(not isinstance(values.get(k), str) for k in _KEYS):
        raise ValueError("lesson must contain five string fields")
    fields = {key: values[key].strip()[:MAX_FIELD_CHARS] for key in _KEYS}
    if not all(fields.values()):
        raise ValueError("repository, failure, root cause, prevention, and evidence are required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", fields["repository"]):
        raise ValueError("repository must use exactly owner/repo format")
    return fields


def _signature(record):
    return tuple(record[key].casefold() for key in ("repository", "failure", "root_cause"))


def _connect(*, create=True):
    """Open a connection without ever replacing an invalid existing database."""
    connection = None
    try:
        path = os.path.abspath(LESSONS_DB)
        if create:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        elif not os.path.isfile(path):
            raise LessonStorageError(f"Lesson database does not exist: {path!r}")
        new_database = not os.path.exists(path)
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        if create:
            connection.execute("BEGIN IMMEDIATE")
            if new_database:
                connection.execute(_SCHEMA)
            connection.execute("SELECT id FROM lessons LIMIT 1").fetchall()
            connection.commit()
        else:
            connection.execute("SELECT id FROM lessons LIMIT 1").fetchall()
        return connection
    except (sqlite3.Error, OSError) as exc:
        if connection is not None:
            connection.close()
        raise LessonStorageError(f"Cannot open lesson database {LESSONS_DB!r}: {exc}") from exc
    except BaseException:
        if connection is not None:
            connection.close()
        raise


def _rollback(connection, operation):
    try:
        connection.rollback()
    except sqlite3.Error as exc:
        raise LessonStorageError(f"{operation} failed; rollback ALSO failed: {exc}") from exc


def _rows(connection):
    return [dict(row) for row in connection.execute(f"SELECT {_COLUMNS} FROM lessons").fetchall()]


def _verified_record(record):
    fields = _validate(record)
    if record.get("status") != "verified":
        raise ValueError("status must be verified")
    for name in ("id", "created_at", "updated_at"):
        if not isinstance(record.get(name), str) or not record[name]:
            raise ValueError(f"{name} must be a nonempty string")
    # Truncation was historical behavior for commands, but migration must not
    # silently truncate old input or introduce new signature collisions.
    if any(record[key] != fields[key] for key in _KEYS):
        raise ValueError("source fields must be trimmed and at most 800 characters; fix a copy")
    return {**fields, "id": record["id"], "status": "verified",
            "created_at": record["created_at"], "updated_at": record["updated_at"]}


def _insert(connection, record):
    connection.execute(
        f"INSERT INTO lessons ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",
        tuple(record[key] for key in ("id", *_KEYS, "status", "created_at", "updated_at")),
    )


def add_verified_lesson(repository, failure, root_cause, prevention, evidence):
    fields = _validate(dict(zip(_KEYS, (repository, failure, root_cause, prevention, evidence))))
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Python casefold covers more Unicode than SQLite's default NOCASE.
        existing = next((row for row in _rows(connection) if _signature(row) == _signature(fields)), None)
        now = datetime.now(timezone.utc).isoformat()
        if existing is None:
            record = {"id": f"lesson-{uuid.uuid4().hex}", **fields,
                      "status": "verified", "created_at": now, "updated_at": now}
            _insert(connection, record)
        else:
            record = {**existing, **fields, "status": "verified", "updated_at": now}
            connection.execute(
                """UPDATE lessons SET repository=?, failure=?, root_cause=?, prevention=?,
                   evidence=?, status='verified', updated_at=? WHERE id=?""",
                (*[fields[k] for k in _KEYS], now, existing["id"]),
            )
        connection.execute(
            """DELETE FROM lessons WHERE id IN
               (SELECT id FROM lessons ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)""",
            (MAX_LESSONS,),
        )
        retained = connection.execute(f"SELECT {_COLUMNS} FROM lessons WHERE id=?", (record["id"],)).fetchone()
        if retained is None:
            raise LessonStorageError("New lesson was removed by retention policy; transaction cancelled")
        connection.commit()
        return dict(retained)
    except sqlite3.Error as exc:
        _rollback(connection, "Saving lesson")
        raise LessonStorageError(f"Cannot save verified lesson in {LESSONS_DB!r}: {exc}") from exc
    except Exception:
        _rollback(connection, "Saving lesson")
        raise
    finally:
        connection.close()


def list_verified_lessons(repository):
    key = (repository or "").strip().casefold()
    if not key:
        return []
    connection = _connect()
    try:
        return sorted(
            [row for row in _rows(connection)
             if row["repository"].casefold() == key and row["status"] == "verified"],
            key=lambda row: row["updated_at"], reverse=True,
        )
    except sqlite3.Error as exc:
        raise LessonStorageError(f"Cannot read verified lessons from {LESSONS_DB!r}: {exc}") from exc
    finally:
        connection.close()


def migrate_json(source=None):
    """Import atomically without pruning, implicit overwrites, or source edits.

    Exact repeated source records sharing an ID/signature are counted as skipped;
    divergent duplicates and collisions with unrelated destination records fail.
    """
    source = source or LESSONS_FILE
    try:
        with open(source, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"Cannot read migration source {source!r}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("lessons"), list):
        raise ValueError("Migration source must be an object with a lessons list")

    source_records = payload["lessons"]
    distinct = {}
    ids = {}
    source_skipped = 0
    for index, raw in enumerate(source_records):
        try:
            record = _verified_record(raw)
        except ValueError as exc:
            raise ValueError(f"Invalid migration record {index}: {exc}") from exc
        signature = _signature(record)
        prior_id = ids.get(record["id"])
        prior_signature = distinct.get(signature)
        if (prior_id is not None and prior_id != record) or (
            prior_signature is not None and prior_signature != record
        ):
            raise ValueError(f"Conflicting source ID or signature at record {index}; correct a copy before migrating")
        if prior_id is not None or prior_signature is not None:
            source_skipped += 1
            continue
        ids[record["id"]] = record
        distinct[signature] = record
    if len(distinct) > MAX_LESSONS:
        raise ValueError(f"Source has {len(distinct)} distinct lessons; maximum is {MAX_LESSONS}. "
                         "Archive/split and reconcile a copy before migration; original unchanged")

    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _rows(connection)
        by_id = {row["id"]: row for row in current}
        by_signature = {_signature(row): row for row in current}
        if len(by_signature) != len(current):
            raise ValueError("Destination contains case-insensitive signature collisions; resolve before migrating")
        inserted = updated = 0
        for signature, record in distinct.items():
            existing_id = by_id.get(record["id"])
            existing_signature = by_signature.get(signature)
            if existing_id and _signature(existing_id) != signature:
                raise ValueError(f"ID collision with different lesson: {record['id']}")
            if existing_signature and existing_signature["id"] != record["id"]:
                raise ValueError(f"Signature collision with different ID: {record['id']}")
            if existing_id or existing_signature:
                updated += 1
            else:
                inserted += 1
        if len(current) + inserted > MAX_LESSONS:
            raise ValueError(f"Migration would exceed {MAX_LESSONS}-lesson limit: "
                             f"{len(current)} existing + {inserted} new. "
                             "Export/backup and reconcile into an empty DB or a smaller source copy")
        for record in distinct.values():
            if record["id"] in by_id:
                connection.execute(
                    """UPDATE lessons SET repository=?, failure=?, root_cause=?, prevention=?,
                       evidence=?, status=?, created_at=?, updated_at=? WHERE id=?""",
                    (*[record[k] for k in _KEYS], record["status"], record["created_at"],
                     record["updated_at"], record["id"]),
                )
            else:
                _insert(connection, record)
        connection.commit()
        return {"source_records": len(source_records), "distinct_signatures": len(distinct),
                "inserted": inserted, "updated": updated,
                "skipped_identical_source_duplicates": source_skipped, "conflicting_records": 0}
    except sqlite3.Error as exc:
        _rollback(connection, "Migration")
        raise LessonStorageError(f"Migration failed; no rows committed: {exc}") from exc
    except Exception:
        _rollback(connection, "Migration")
        raise
    finally:
        connection.close()


def export_json(destination, *, overwrite=False):
    """Export a consistent SQLite snapshot; refuse overwrite unless explicit.

    Export does not modify SQLite. A temporary file and atomic rename protect
    completed exports. Without overwrite, exclusive creation protects existing files.
    """
    connection = _connect(create=False)
    try:
        connection.execute("BEGIN")
        records = _rows(connection)
        connection.commit()
    except sqlite3.Error as exc:
        _rollback(connection, "Export read")
        raise LessonStorageError(f"Cannot export lessons from {LESSONS_DB!r}: {exc}") from exc
    finally:
        connection.close()

    if os.path.abspath(destination) == os.path.abspath(LESSONS_DB):
        raise ValueError("Export destination must not be the SQLite database")
    import tempfile
    temporary = None
    try:
        parent = os.path.dirname(os.path.abspath(destination))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=parent,
                                         prefix=".lessons-export-", delete=False) as handle:
            temporary = handle.name
            json.dump({"lessons": records}, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, destination)
        else:
            # Exclusive creation avoids overwriting even when two exports race.
            os.link(temporary, destination)
        return len(records)
    except (OSError, UnicodeError) as exc:
        raise LessonStorageError(f"Cannot write export {destination!r}: {exc}") from exc
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verified lesson database maintenance")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--migrate-json", metavar="PATH")
    mode.add_argument("--export-json", metavar="PATH")
    parser.add_argument("--overwrite-export", action="store_true",
                        help="Replace an existing export only after making a backup")
    args = parser.parse_args()
    if args.migrate_json:
        if args.overwrite_export:
            parser.error("--overwrite-export only applies to --export-json")
        print(json.dumps(migrate_json(args.migrate_json), indent=2))
    else:
        print(f"Exported {export_json(args.export_json, overwrite=args.overwrite_export)} lessons to {args.export_json}")
