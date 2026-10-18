"""Offline tests for transactional verified lessons."""
import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import learning
from integrations import lessons as discord_lessons


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "lessons.sqlite3")
        self.json_path = os.path.join(self.temp.name, "lessons.json")
        self.patch = patch.object(learning, "LESSONS_DB", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def add(self, repo="owner/repo", failure="CI import error", cause="missing module",
            prevention="add module and test", evidence="CI passed after commit abc123"):
        return learning.add_verified_lesson(repo, failure, cause, prevention, evidence)

    def test_persists_across_connections(self):
        self.add()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lessons").fetchone()[0], 1)
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)

    def test_isolation_and_validation(self):
        self.add()
        self.assertEqual(learning.list_verified_lessons("owner/other"), [])
        for repo in ("repo", "owner/", "/repo", "owner/repo/extra"):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                self.add(repo=repo)
        with self.assertRaises(ValueError):
            self.add(evidence="")

    def test_duplicate_updates(self):
        first = self.add()
        second = self.add(repo="OWNER/REPO", failure="ci IMPORT ERROR",
                          cause="MISSING MODULE", prevention="better fix")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)
        self.assertEqual(learning.list_verified_lessons("owner/repo")[0]["prevention"], "better fix")

    def test_retention(self):
        for i in range(510):
            self.add(failure=f"failure-{i}")
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 500)

    def write_json(self, records):
        with open(self.json_path, "w", encoding="utf-8") as handle:
            json.dump({"lessons": records}, handle)

    def test_migration_and_rerun(self):
        lesson = self.add()
        self.write_json([lesson])
        original = open(self.json_path, "rb").read()
        self.assertEqual(learning.migrate_json(self.json_path), 1)
        self.assertEqual(learning.migrate_json(self.json_path), 1)
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)
        self.assertEqual(open(self.json_path, "rb").read(), original)

    def test_malformed_and_invalid_migration(self):
        with open(self.json_path, "w") as handle:
            handle.write("{broken")
        with self.assertRaises(ValueError):
            learning.migrate_json(self.json_path)
        self.assertEqual(open(self.json_path).read(), "{broken")
        self.write_json([{"repository": "owner/repo"}])
        with self.assertRaisesRegex(ValueError, "record 0"):
            learning.migrate_json(self.json_path)
        self.assertEqual(learning.list_verified_lessons("owner/repo"), [])

    def test_migration_transaction_rollback(self):
        a = self.add(failure="one")
        b = self.add(failure="two")
        b["id"] = a["id"]
        self.write_json([a, b])
        with self.assertRaises(learning.LessonStorageError):
            learning.migrate_json(self.json_path)
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 2)

    def test_storage_error_visible(self):
        with patch.object(learning, "LESSONS_DB", self.temp.name):
            with self.assertRaises(learning.LessonStorageError):
                self.add()
            with self.assertRaises(learning.LessonStorageError):
                learning.list_verified_lessons("owner/repo")

    def test_concurrent_writes(self):
        errors = []
        def write(i):
            try:
                self.add(failure=f"parallel-{i}")
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 20)

    def test_discord_boundaries(self):
        for i in range(8):
            self.add(repo="a" * 100 + "/" + "b" * 100,
                     failure="x" * 800 + str(i), cause="c" * 800,
                     prevention="p" * 800, evidence="e" * 800)
        repo = "a" * 100 + "/" + "b" * 100
        reply = asyncio.run(discord_lessons.handle(None, {"action": "list", "repository": repo}))
        self.assertLessEqual(len(reply), 2000)
        self.assertIn("Showing", reply)
        saved = asyncio.run(discord_lessons.handle(None, {"action": "add",
            "repository": repo, "failure": "f" * 800, "root_cause": "c" * 800,
            "prevention": "p" * 800, "evidence": "e" * 800}))
        self.assertLessEqual(len(saved), 2000)


if __name__ == "__main__":
    unittest.main()
