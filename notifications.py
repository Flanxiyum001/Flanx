"""Background Task & Notification System for Max.

Manages long-running tasks asynchronously and dispatches completion notifications
via Discord channel messages, DMs, or Email.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from integrations.email import send_email

NOTIF_DB = os.getenv("MAX_NOTIF_DB") or os.path.join(
    os.path.dirname(__file__), ".max_state", "notifications.sqlite3"
)


@dataclass
class BackgroundTask:
    task_id: str
    user_id: int
    channel_id: int
    task_type: str
    description: str
    status: str  # pending, running, completed, failed
    notify_channel: bool
    notify_dm: bool
    notify_email: str | None
    created_at: str
    updated_at: str
    result_summary: str | None = None


class NotificationSystem:
    def __init__(self):
        self._init_db()

    def _init_db(self) -> None:
        Path(NOTIF_DB).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(NOTIF_DB)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS background_tasks (
                    task_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    task_type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    notify_channel INTEGER NOT NULL,
                    notify_dm INTEGER NOT NULL,
                    notify_email TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_summary TEXT
                )
            """)

    def create_task(
        self,
        user_id: int,
        channel_id: int,
        task_type: str,
        description: str,
        notify_channel: bool = True,
        notify_dm: bool = True,
        notify_email: str | None = None,
    ) -> BackgroundTask:
        task_id = f"bg_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        task = BackgroundTask(
            task_id=task_id,
            user_id=user_id,
            channel_id=channel_id,
            task_type=task_type,
            description=description,
            status="pending",
            notify_channel=notify_channel,
            notify_dm=notify_dm,
            notify_email=notify_email,
            created_at=now,
            updated_at=now,
        )
        with closing(sqlite3.connect(NOTIF_DB)) as db, db:
            db.execute(
                """
                INSERT INTO background_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    task.task_id,
                    task.user_id,
                    task.channel_id,
                    task.task_type,
                    task.description,
                    task.status,
                    1 if task.notify_channel else 0,
                    1 if task.notify_dm else 0,
                    task.notify_email,
                    task.created_at,
                    task.updated_at,
                    task.result_summary,
                ),
            )
        return task

    def update_task_status(
        self, task_id: str, status: str, result_summary: str | None = None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(NOTIF_DB)) as db, db:
            db.execute(
                """
                UPDATE background_tasks
                SET status = ?, result_summary = ?, updated_at = ?
                WHERE task_id = ?
            """,
                (status, result_summary, now, task_id),
            )

    def get_task(self, task_id: str) -> BackgroundTask | None:
        with closing(sqlite3.connect(NOTIF_DB)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM background_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                return None
            return BackgroundTask(
                task_id=row["task_id"],
                user_id=row["user_id"],
                channel_id=row["channel_id"],
                task_type=row["task_type"],
                description=row["description"],
                status=row["status"],
                notify_channel=bool(row["notify_channel"]),
                notify_dm=bool(row["notify_dm"]),
                notify_email=row["notify_email"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                result_summary=row["result_summary"],
            )

    async def notify_completion(self, bot, task: BackgroundTask) -> None:
        """Dispatch notifications across configured channels."""
        status_emoji = "✅" if task.status == "completed" else "❌"
        msg = f"{status_emoji} **Background Task Finished**: `{task.description}`\nStatus: `{task.status}`\n"
        if task.result_summary:
            msg += f"Summary:\n```\n{task.result_summary[:1500]}\n```"

        # 1. Channel Notification
        if task.notify_channel and task.channel_id:
            try:
                channel = bot.get_channel(task.channel_id)
                if channel:
                    await channel.send(msg)
            except Exception as e:
                print(f"Failed to send channel notification: {e}")

        # 2. DM Notification
        if task.notify_dm and task.user_id:
            try:
                user = await bot.fetch_user(task.user_id)
                if user:
                    await user.send(f"📬 **Direct Task Update**\n{msg}")
            except Exception as e:
                print(f"Failed to send DM notification: {e}")

        # 3. Email Notification
        if task.notify_email:
            subject = f"Max Task {task.status.title()}: {task.description[:50]}"
            body = (
                f"Task ID: {task.task_id}\n"
                f"Description: {task.description}\n"
                f"Status: {task.status}\n\n"
                f"Result:\n{task.result_summary or 'No additional details.'}\n"
            )
            send_email(task.notify_email, subject, body)


notification_system = NotificationSystem()


def get_notification_system() -> NotificationSystem:
    return notification_system
