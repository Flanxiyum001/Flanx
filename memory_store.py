"""Scoped, inspectable memory. Proposed records never become verified implicitly."""
from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


_CATEGORIES = {"preference", "project_fact", "procedure", "technical_finding",
               "known_error", "task_context"}
_CREDENTIAL = re.compile(
    r"(?i)(?:api[_-]?key|password|secret|token|authorization|cookie)\s*[:=]|"
    r"(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}")


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, category TEXT NOT NULL, scope TEXT NOT NULL,
                content TEXT NOT NULL, source TEXT NOT NULL, confidence REAL NOT NULL,
                status TEXT NOT NULL, project TEXT, task_id TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, expires_at TEXT,
                UNIQUE(category, scope, content))""")
            db.execute("""CREATE TABLE IF NOT EXISTS task_feedback (
                task_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, evidence TEXT NOT NULL,
                created_at TEXT NOT NULL)""")

    @staticmethod
    def _safe(value: str, limit: int = 800) -> str:
        if (not isinstance(value, str) or not value.strip() or len(value) > limit
                or _CREDENTIAL.search(value)):
            raise ValueError("Invalid or sensitive memory content")
        return value.strip()

    def add(self, *, category: str, scope: str, content: str, source: str,
            confidence: float, status: str = "proposed", project: str | None = None,
            task_id: str | None = None, expires_at: str | None = None) -> dict:
        if category not in _CATEGORIES or status not in {"proposed", "verified", "rejected"}:
            raise ValueError("Unknown memory category or status")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("Confidence must be between zero and one")
        scope, content, source = self._safe(scope, 120), self._safe(content), self._safe(source, 200)
        if project is not None:
            project = self._safe(project, 120)
        if task_id is not None:
            task_id = self._safe(task_id, 120)
        if expires_at is not None:
            datetime.fromisoformat(expires_at)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.path)) as db, db:
            existing = db.execute("SELECT id FROM memories WHERE category=? AND scope=? AND content=?",
                                  (category, scope, content)).fetchone()
            if existing:
                return self.get(existing[0])
            record_id = uuid.uuid4().hex
            db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (record_id, category, scope, content, source, float(confidence), status,
                        project, task_id, now, now, expires_at))
        return self.get(record_id)

    def get(self, record_id: str) -> dict | None:
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM memories WHERE id=?", (record_id,)).fetchone()
        return dict(row) if row else None

    def list(self, *, scope: str | None = None, limit: int = 50) -> list[dict]:
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM memories WHERE (? IS NULL OR scope=?) "
                              "AND (expires_at IS NULL OR expires_at>?) "
                              "ORDER BY updated_at DESC LIMIT ?",
                              (scope, scope, datetime.now(timezone.utc).isoformat(),
                               max(1, min(limit, 100)))).fetchall()
        return [dict(row) for row in rows]

    def correct(self, record_id: str, *, content: str, source: str) -> dict:
        content, source = self._safe(content), self._safe(source, 200)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.path)) as db, db:
            cursor = db.execute("UPDATE memories SET content=?, source=?, status='proposed', "
                                "confidence=0, updated_at=? WHERE id=?",
                                (content, source, now, record_id))
            if cursor.rowcount != 1:
                raise KeyError(record_id)
        return self.get(record_id)

    def verify(self, record_id: str, *, evidence: str, confidence: float) -> dict:
        evidence = self._safe(evidence, 200)
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("Invalid confidence")
        with closing(sqlite3.connect(self.path)) as db, db:
            cursor = db.execute("UPDATE memories SET source=?, confidence=?, status='verified', "
                                "updated_at=? WHERE id=?", (evidence, float(confidence),
                                datetime.now(timezone.utc).isoformat(), record_id))
            if cursor.rowcount != 1:
                raise KeyError(record_id)
        return self.get(record_id)

    def delete(self, record_id: str) -> bool:
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute("DELETE FROM memories WHERE id=?", (record_id,)).rowcount == 1

    def feedback(self, task_id: str, outcome: str, evidence: str) -> None:
        if outcome not in {"succeeded", "failed", "corrected", "rejected"}:
            raise ValueError("Invalid feedback")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO task_feedback VALUES (?,?,?,?) ON CONFLICT(task_id) "
                       "DO UPDATE SET outcome=excluded.outcome,evidence=excluded.evidence,"
                       "created_at=excluded.created_at",
                       (self._safe(task_id, 120), outcome, self._safe(evidence, 200),
                        datetime.now(timezone.utc).isoformat()))
