"""Atomic SQLite storage for bounded, non-sensitive task metadata."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path


class TaskStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.available = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                with db:
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS tasks "
                        "(task_id TEXT PRIMARY KEY, record TEXT NOT NULL)"
                    )
        except (OSError, sqlite3.Error):
            self.available = False

    def load(self) -> list[dict]:
        if not self.available:
            return []
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                rows = db.execute("SELECT task_id, record FROM tasks").fetchall()
        except sqlite3.Error:
            self.available = False
            return []
        records = []
        for task_id, raw in rows:
            if not isinstance(raw, str) or len(raw) > 2_000:
                continue
            try:
                record = json.loads(raw)
                if (isinstance(record, dict) and record.get("schema") == 1
                        and record.get("task_id") == task_id):
                    records.append(record)
            except (TypeError, ValueError):
                continue
        return records

    def save(self, record: dict) -> bool:
        if not self.available:
            return False
        try:
            encoded = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
            if len(encoded) > 2_000:
                return False
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                with db:
                    db.execute("INSERT INTO tasks(task_id, record) VALUES(?, ?) "
                               "ON CONFLICT(task_id) DO UPDATE SET record=excluded.record",
                               (record["task_id"], encoded))
            return True
        except (OSError, sqlite3.Error, TypeError, ValueError, KeyError):
            self.available = False
            return False

    def delete(self, task_id: str) -> bool:
        if not self.available:
            return False
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                with db:
                    db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            return True
        except (OSError, sqlite3.Error):
            self.available = False
            return False
