"""Non-destructive inventory and independently verified backups of Max state.

Run from the same working directory and storage configuration as the bot:
    python storage_admin.py --inventory
    python storage_admin.py --backup-dir /safe/new-backup-directory
    python storage_admin.py --verify-backup /safe/new-backup-directory

A missing file is not evidence that historical records never existed.
A completed backup must be copied off any ephemeral container before cutover.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATE_PATHS = {
    "lessons_db": ("MAX_LESSONS_DB", "max_lessons.sqlite3"),
    "legacy_lessons_json": ("MAX_LESSONS_FILE", "max_lessons.json"),
    "preferences": ("FLANX_PREFS_FILE", "flanx_prefs.json"),
    "usage": ("FLANX_USAGE_FILE", "flanx_usage.json"),
}
# Retained for compatibility with existing callers/tests that patch this mapping.
FILES = {name: os.getenv(variable, default) for name, (variable, default) in STATE_PATHS.items()}
MANIFEST_VERSION = 1
LESSON_COLUMNS = {
    "id", "repository", "failure", "root_cause", "prevention",
    "evidence", "status", "created_at", "updated_at",
}


def _paths() -> dict[str, str]:
    """Read environment overrides at call time, not only at module import."""
    return {
        name: os.getenv(variable, FILES[name])
        for name, (variable, _) in STATE_PATHS.items()
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nonnegative_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_json(name: str, path: Path) -> dict:
    """Return counts only; never return or log state contents."""
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")

    if name == "legacy_lessons_json":
        records = value.get("lessons")
        if not isinstance(records, list):
            raise ValueError("Legacy lessons must contain a lessons list")
        # Match the explicit importer rather than accepting unimportable records.
        from learning import _verified_record
        for record in records:
            _verified_record(record)
        return {"record_count": len(records)}

    if name == "preferences":
        users = value.get("users")
        if set(value) != {"users"} or not isinstance(users, dict):
            raise ValueError("Invalid preferences structure")
        notes = 0
        for user in users.values():
            if (not isinstance(user, dict)
                    or not isinstance(user.get("prefs"), dict)
                    or not isinstance(user.get("notes"), list)
                    or any(not isinstance(note, str) for note in user["notes"])):
                raise ValueError("Invalid user preferences/notes structure")
            notes += len(user["notes"])
        return {"record_count": len(users), "note_count": notes}

    if name == "usage":
        llm, sandbox = value.get("llm"), value.get("sandbox")
        if not isinstance(llm, dict) or not isinstance(sandbox, dict):
            raise ValueError("Invalid usage structure")
        for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens"):
            if key in llm and not _nonnegative_int(llm[key]):
                raise ValueError("Invalid usage counter")
        cost = llm.get("cost_usd", 0)
        if (isinstance(cost, bool) or not isinstance(cost, (int, float))
                or not math.isfinite(cost) or cost < 0):
            raise ValueError("Invalid usage cost")
        models = llm.get("by_model", {})
        if not isinstance(models, dict):
            raise ValueError("Invalid usage models")
        for counters in models.values():
            if (not isinstance(counters, dict)
                    or any(not _nonnegative_int(counters.get(k, 0))
                           for k in ("calls", "prompt_tokens", "completion_tokens", "total_tokens"))):
                raise ValueError("Invalid per-model counter")
        total_runs = 0
        for counters in sandbox.values():
            if (not isinstance(counters, dict)
                    or any(not _nonnegative_int(counters.get(k, 0))
                           for k in ("runs", "passed", "failed"))):
                raise ValueError("Invalid sandbox counter")
            total_runs += counters.get("runs", 0)
        calls = llm.get("calls", 0)
        return {
            "record_count": calls + total_runs,
            "llm_calls": calls, "sandbox_runs": total_runs,
            "sandbox_months": len(sandbox),
        }
    raise ValueError("Unknown state file")


def _sqlite_details(path: Path) -> dict:
    """Check the live source in read-only mode without creating a missing DB."""
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(lessons)")}
        if not LESSON_COLUMNS.issubset(columns):
            raise ValueError("Invalid SQLite lessons schema")
        count = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    return {"record_count": count, "sqlite_integrity": "ok"}


def _inspect(name: str, raw_path: str, expected_existing: bool = False) -> dict:
    item = {
        "name": name, "configured_path": raw_path,
        "exists": False, "readable": False,
        "validation": "not_checked",
    }
    if not raw_path or not isinstance(raw_path, str):
        return {**item, "status": "invalid_path"}
    path = Path(os.path.abspath(raw_path))
    item["path"] = str(path)
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        try:
            parent_is_dir = path.parent.is_dir()
        except OSError:
            parent_is_dir = False
        if not parent_is_dir:
            return {**item, "status": "invalid_path", "reason": "parent_missing_or_inaccessible"}
        return {
            **item, "status": "missing_expected" if expected_existing else "missing",
            "history": "unknown", "validation": "not_applicable",
        }
    except (OSError, ValueError):
        return {**item, "status": "inaccessible"}
    if not stat.S_ISREG(file_stat.st_mode):
        return {**item, "exists": True, "status": "invalid_path",
                "reason": "not_a_regular_file"}
    item.update(exists=True, bytes=file_stat.st_size)
    try:
        if name == "lessons_db":
            counts = _sqlite_details(path)
        else:
            counts = _validate_json(name, path)
        item["sha256"] = _sha256(path)
    except (PermissionError, OSError, sqlite3.OperationalError):
        return {**item, "status": "unreadable", "validation": "failed"}
    except (ValueError, UnicodeError, sqlite3.DatabaseError):
        return {**item, "readable": True, "status": "corrupt",
                "validation": "failed"}
    item.update(counts)
    item["readable"] = True
    item["validation"] = "valid"
    item["status"] = "valid_empty" if counts["record_count"] == 0 else "valid_records"
    if name == "lessons_db":
        # WAL data is included in the online backup, but not in this main-file hash.
        item["sha256_scope"] = "database_main_file_only_not_consistent_snapshot"
    return item


def inventory(*, expected_existing=()) -> list[dict]:
    """Read-only inventory; returns a status for every configured store."""
    expected = set(expected_existing)
    if not expected.issubset(STATE_PATHS):
        raise ValueError("Unknown expected store")
    return [
        _inspect(name, raw, name in expected)
        for name, raw in _paths().items()
    ]


def _write_manifest(target: Path, manifest: dict) -> None:
    """Flush a new manifest and atomically publish it in the backup directory."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=target, encoding="utf-8",
            prefix=".manifest-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target / "manifest.json")
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def backup(destination: str, *, expected_existing=()) -> dict:
    """Back up real, validated files; refuse invalid sources and old destinations."""
    source_inventory = inventory(expected_existing=expected_existing)
    for item in source_inventory:
        if item["status"] not in {"missing", "valid_empty", "valid_records"}:
            raise ValueError(
                "Backup preflight failed for " + item["name"] + ": " + item["status"]
            )
    target = Path(destination).resolve()
    if target.exists() or target.is_symlink():
        raise FileExistsError("Backup destination already exists")
    target.mkdir(parents=True, exist_ok=False)
    manifest = {
        "format_version": MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "complete": False, "files": [],
    }
    try:
        for item in source_inventory:
            name = item["name"]
            source = Path(item["path"])
            if item["status"] == "missing":
                manifest["files"].append({
                    "name": name, "source": str(source),
                    "status": "missing", "history": "unknown",
                    "validation": "not_applicable",
                })
                continue
            filename = name + (source.suffix or ".bin")
            output = target / filename
            if name == "lessons_db":
                with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
                    with sqlite3.connect(output) as dst:
                        src.backup(dst)
                details = _sqlite_details(output)
            else:
                shutil.copy2(source, output)
                details = _validate_json(name, output)
                if (_sha256(source) != item["sha256"]
                        or _sha256(output) != item["sha256"]):
                    raise OSError("JSON source changed during backup: " + name)
            manifest["files"].append({
                "name": name, "source": str(source), "backup": filename,
                "status": "backed_up", "validation": "valid",
                "bytes": output.stat().st_size,
                "sha256": _sha256(output), **details,
            })
        manifest["complete"] = True
        _write_manifest(target, manifest)
        verify_backup(str(target))
        return manifest
    except Exception:
        # Never delete a partial copy; label its manifest as incomplete.
        manifest["complete"] = False
        try:
            _write_manifest(target, manifest)
        except OSError:
            pass
        raise


def verify_backup(directory: str) -> dict:
    """Verify backed-up copies only; never read production source paths."""
    target = Path(directory).resolve()
    with (target / "manifest.json").open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if (not isinstance(manifest, dict)
            or manifest.get("format_version") != MANIFEST_VERSION
            or manifest.get("complete") is not True
            or not isinstance(manifest.get("created_at"), str)
            or not isinstance(manifest.get("files"), list)):
        raise ValueError("Invalid or incomplete backup manifest")
    expected = set(STATE_PATHS)
    seen, backup_names = set(), set()
    verified = missing = 0
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or entry.get("name") not in expected:
            raise ValueError("Unknown backup manifest entry")
        name = entry["name"]
        if name in seen or not isinstance(entry.get("source"), str):
            raise ValueError("Duplicate or invalid backup manifest entry")
        seen.add(name)
        if entry.get("status") == "missing":
            if "backup" in entry or entry.get("history") != "unknown":
                raise ValueError("Invalid missing-source entry")
            missing += 1
            continue
        if entry.get("status") != "backed_up" or entry.get("validation") != "valid":
            raise ValueError("Incomplete backup manifest entry")
        filename = entry.get("backup")
        if (not isinstance(filename, str) or Path(filename).name != filename
                or filename in {"", ".", "..", "manifest.json"} or filename in backup_names):
            raise ValueError("Invalid or duplicate backup filename")
        backup_names.add(filename)
        path = target / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing or invalid backup file: " + name)
        digest = entry.get("sha256")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not _nonnegative_int(entry.get("bytes"))
                or path.stat().st_size != entry["bytes"]
                or _sha256(path) != digest):
            raise ValueError("Backup size/checksum mismatch: " + name)
        if name == "lessons_db":
            details = _sqlite_details(path)
        else:
            details = _validate_json(name, path)
        for key, value in details.items():
            if entry.get(key) != value:
                raise ValueError("Backup validation/count mismatch: " + name)
        verified += 1
    if seen != expected:
        raise ValueError("Backup manifest omits expected stores")
    if verified == 0:
        raise ValueError("Backup contains no source data; not a recovery point")
    if {p.name for p in target.iterdir()} != backup_names | {"manifest.json"}:
        raise ValueError("Unexpected files in backup directory")
    return {"status": "verified", "files_verified": verified, "sources_missing": missing}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--inventory", action="store_true")
    actions.add_argument("--backup-dir")
    actions.add_argument("--verify-backup")
    parser.add_argument(
        "--expect-existing", action="append", default=[],
        choices=tuple(STATE_PATHS),
        help="Require a previously known store to exist during inventory/backup",
    )
    args = parser.parse_args()
    if args.verify_backup:
        result = verify_backup(args.verify_backup)
    elif args.inventory:
        result = inventory(expected_existing=args.expect_existing)
    else:
        result = backup(args.backup_dir, expected_existing=args.expect_existing)
    print(json.dumps(result, indent=2))
    if args.inventory and any(
        item["status"] not in {"missing", "valid_empty", "valid_records"}
        for item in result
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
