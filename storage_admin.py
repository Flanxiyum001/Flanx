"""Inventory and make verified, non-destructive backups of Flanx local state.

Run from the repository root before attaching/moving storage:
    python storage_admin.py --inventory
    python storage_admin.py --backup-dir /path/to/a/new/backup-directory

The backup destination must not already exist. SQLite is copied through SQLite's
online backup API; JSON is copied byte-for-byte after validating it parses. No
source file is modified and no state contents are printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

FILES = {
    "lessons_db": os.getenv("MAX_LESSONS_DB", "max_lessons.sqlite3"),
    "legacy_lessons_json": os.getenv("MAX_LESSONS_FILE", "max_lessons.json"),
    "preferences": os.getenv("FLANX_PREFS_FILE", "flanx_prefs.json"),
    "usage": os.getenv("FLANX_USAGE_FILE", "flanx_usage.json"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_json(name: str, path: Path) -> None:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if name == "preferences" and (
        not isinstance(value, dict) or set(value) != {"users"} or not isinstance(value["users"], dict)
    ):
        raise ValueError(f"{path}: invalid preferences structure")
    if name == "usage" and (
        not isinstance(value, dict) or not isinstance(value.get("llm"), dict)
        or not isinstance(value.get("sandbox"), dict)
    ):
        raise ValueError(f"{path}: invalid usage structure")
    if name == "legacy_lessons_json" and (
        not isinstance(value, dict) or not isinstance(value.get("lessons"), list)
    ):
        raise ValueError(f"{path}: invalid legacy lessons structure")


def inventory() -> list[dict]:
    seen = set()
    items = []
    for name, raw_path in FILES.items():
        path = Path(raw_path).resolve()
        if path in seen:
            continue
        seen.add(path)
        item = {"name": name, "path": str(path), "exists": path.is_file()}
        if path.is_file():
            item["bytes"] = path.stat().st_size
            item["sha256"] = _sha256(path)
            if name.endswith("json") or name in {"preferences", "usage"}:
                _validate_json(name, path)
            elif name == "lessons_db":
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                    check = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    item["sqlite_integrity"] = check
                    item["lesson_count"] = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
                    if check != "ok":
                        raise ValueError(f"{path}: SQLite integrity check failed: {check}")
        items.append(item)
    return items


def backup(destination: str) -> dict:
    target = Path(destination).resolve()
    if target.exists():
        raise FileExistsError(f"Backup destination already exists: {target}")
    target.mkdir(parents=True, exist_ok=False)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [],
    }
    try:
        for name, raw_path in FILES.items():
            source = Path(raw_path).resolve()
            if not source.is_file():
                manifest["files"].append({"name": name, "source": str(source), "status": "missing"})
                continue
            if name in {"preferences", "usage", "legacy_lessons_json"}:
                _validate_json(name, source)
            filename = f"{name}{source.suffix or '.bin'}"
            output = target / filename
            if name == "lessons_db":
                with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
                    with sqlite3.connect(output) as dst:
                        src.backup(dst)
                        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
                        if check != "ok":
                            raise ValueError(f"Backup SQLite integrity check failed: {check}")
                        count = dst.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
                extra = {"sqlite_integrity": check, "lesson_count": count}
            else:
                shutil.copy2(source, output)
                if _sha256(source) != _sha256(output):
                    raise IOError(f"Backup checksum mismatch for {source}")
                extra = {"sha256": _sha256(output)}
            manifest["files"].append({
                "name": name, "source": str(source), "backup": filename,
                "status": "backed_up", "bytes": output.stat().st_size, **extra,
            })
        manifest_path = target / "manifest.json"
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        return manifest
    except Exception:
        # Keep the partial backup directory for forensic inspection; never remove
        # files that might be the only recoverable copy of data.
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--inventory", action="store_true")
    group.add_argument("--backup-dir")
    args = parser.parse_args()
    result = inventory() if args.inventory else backup(args.backup_dir)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
