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
        with open(self.json_path, "rb") as handle:
            original = handle.read()
        expected = {"source_records": 1, "distinct_signatures": 1,
                    "inserted": 0, "updated": 1,
                    "skipped_identical_source_duplicates": 0, "conflicting_records": 0}
        first = learning.migrate_json(self.json_path)
        second = learning.migrate_json(self.json_path)
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)
        with open(self.json_path, "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_migration_new_database_and_idempotent_rerun(self):
        source_lesson = self.add()
        self.write_json([source_lesson, source_lesson])
        with patch.object(learning, "LESSONS_DB", os.path.join(self.temp.name, "import.sqlite")):
            first = learning.migrate_json(self.json_path)
            self.assertEqual(first, {"source_records": 2, "distinct_signatures": 1,
                                     "inserted": 1, "updated": 0,
                                     "skipped_identical_source_duplicates": 1,
                                     "conflicting_records": 0})
            second = learning.migrate_json(self.json_path)
            self.assertEqual(second, {**first, "inserted": 0, "updated": 1})
            stored = learning.list_verified_lessons("owner/repo")
            self.assertEqual([row["id"] for row in stored], [source_lesson["id"]])

    def test_malformed_and_invalid_migration(self):
        with open(self.json_path, "w") as handle:
            handle.write("{broken")
        with self.assertRaises(ValueError):
            learning.migrate_json(self.json_path)
        with open(self.json_path) as handle:
            self.assertEqual(handle.read(), "{broken")
        self.write_json([{"repository": "owner/repo"}])
        with self.assertRaisesRegex(ValueError, "record 0"):
            learning.migrate_json(self.json_path)
        self.assertEqual(learning.list_verified_lessons("owner/repo"), [])

    def test_migration_transaction_rollback(self):
        a = self.add(failure="one")
        b = self.add(failure="two")
        before = learning.list_verified_lessons("owner/repo")
        b["id"] = a["id"]
        self.write_json([a, b])
        with self.assertRaisesRegex(ValueError, "Conflicting source ID"):
            learning.migrate_json(self.json_path)
        self.assertEqual(learning.list_verified_lessons("owner/repo"), before)

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


    def test_first_concurrent_writes_wait_for_schema_initialization(self):
        """A second writer must not use the newly created file before DDL commits.

        Intercept the first sqlite connect *after it creates the file* and
        wait for the other writer to finish (or time out). Without the
        initialization lock the second writer sees 'no such table: lessons'.
        """
        self.assertFalse(os.path.exists(self.path))
        start = threading.Barrier(3)
        other_finished = threading.Event()
        first_connected = threading.Event()
        capture_lock = threading.Lock()
        first = [True]
        errors = []
        results = []
        real_connect = sqlite3.connect

        def gated_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            with capture_lock:
                is_first = first[0]
                first[0] = False
            if is_first:
                first_connected.set()
                # The peer cannot finish while initialization is locked.
                # In the buggy code, it finishes with "no such table".
                other_finished.wait(timeout=2)
            return connection

        def write(number):
            try:
                start.wait(timeout=5)
                results.append(self.add(failure=f"first-wave-{number}"))
            except BaseException as exc:
                errors.append(exc)
            finally:
                if not first_connected.is_set():
                    first_connected.set()
                else:
                    other_finished.set()

        threads = [threading.Thread(target=write, args=(i,)) for i in range(2)]
        try:
            with patch.object(learning.sqlite3, "connect", side_effect=gated_connect):
                for thread in threads:
                    thread.start()
                start.wait(timeout=5)
                self.assertTrue(first_connected.wait(timeout=5))
                for thread in threads:
                    thread.join(timeout=10)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
        finally:
            start.abort()
            other_finished.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({row["id"] for row in results}), 2)
        stored = learning.list_verified_lessons("owner/repo")
        self.assertCountEqual([row["failure"] for row in stored],
                              ["first-wave-0", "first-wave-1"])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='lessons'"
            ).fetchone()[0], "lessons")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lessons").fetchone()[0], 2)

    def test_migration_rejects_more_than_500_without_changes(self):
        base = self.add()
        source = [{**base, "id": f"id-{i}", "failure": f"issue-{i}"}
                  for i in range(501)]
        self.write_json(source)
        with open(self.json_path, "rb") as handle:
            before = handle.read()
        with self.assertRaisesRegex(ValueError, "maximum is 500"):
            learning.migrate_json(self.json_path)
        with open(self.json_path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)

    def test_identical_duplicates_are_reported(self):
        record = self.add()
        self.write_json([record, record])
        result = learning.migrate_json(self.json_path)
        self.assertEqual(result["source_records"], 2)
        self.assertEqual(result["distinct_signatures"], 1)
        self.assertEqual(result["skipped_identical_source_duplicates"], 1)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["updated"], 1)

    def test_conflicting_signatures_rejected_before_writes(self):
        record = self.add()
        copy = {**record, "id": "other-id"}
        self.write_json([record, copy])
        with self.assertRaisesRegex(ValueError, "Conflicting source"):
            learning.migrate_json(self.json_path)
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)

    def test_collision_with_destination_id_and_signature(self):
        original = self.add()
        self.write_json([{**original, "failure": "another failure"}])
        with self.assertRaisesRegex(ValueError, "ID collision"):
            learning.migrate_json(self.json_path)
        self.write_json([{**original, "id": "different-id"}])
        with self.assertRaisesRegex(ValueError, "Signature collision"):
            learning.migrate_json(self.json_path)
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)

    def test_capacity_with_existing_destination(self):
        original = self.add()
        for i in range(499):
            self.add(failure=f"existing-{i}")
        self.write_json([{**original, "id": "new-id", "failure": "not present"}])
        with self.assertRaisesRegex(ValueError, "exceed 500"):
            learning.migrate_json(self.json_path)
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 500)

    def test_export_is_read_only_and_prevents_overwrite(self):
        self.add()
        exported = os.path.join(self.temp.name, "export.json")
        self.assertEqual(learning.export_json(exported), 1)
        with open(exported, encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)["lessons"]), 1)
        with open(exported, "rb") as handle:
            original = handle.read()
        with self.assertRaises(learning.LessonStorageError):
            learning.export_json(exported)
        with open(exported, "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertEqual(learning.export_json(exported, overwrite=True), 1)
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)

    def test_export_cannot_replace_sqlite_using_alias(self):
        self.add()
        alias = os.path.join(self.temp.name, "database-alias.json")
        os.link(self.path, alias)
        with self.assertRaisesRegex(ValueError, "SQLite database"):
            learning.export_json(alias, overwrite=True)
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)
        self.assertEqual(os.listdir(self.temp.name).count("database-alias.json"), 1)
        self.assertFalse(any(name.startswith(".lessons-export-")
                             for name in os.listdir(self.temp.name)))

    def test_failed_export_cleans_temporary_file(self):
        self.add()
        destination = os.path.join(self.temp.name, "export.json")
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write("original content")
        with self.assertRaises(learning.LessonStorageError):
            learning.export_json(destination)
        with open(destination, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "original content")
        self.assertFalse(any(name.startswith(".lessons-export-")
                             for name in os.listdir(self.temp.name)))

    def test_export_refuses_missing_or_invalid_database(self):
        with patch.object(learning, "LESSONS_DB", os.path.join(self.temp.name, "missing.sqlite")):
            with self.assertRaises(learning.LessonStorageError):
                learning.export_json(os.path.join(self.temp.name, "export.json"))
        with patch.object(learning, "LESSONS_DB", os.path.join(self.temp.name, "bad.sqlite")):
            with open(learning.LESSONS_DB, "wb") as handle:
                handle.write(b"not a sqlite database")
            with self.assertRaises(learning.LessonStorageError):
                learning.list_verified_lessons("owner/repo")

    def test_rerunning_stale_json_cannot_overwrite_newer_sqlite_lesson(self):
        older = self.add(failure="stale-legacy-source", prevention="old fix")
        self.write_json([older])
        newer = self.add(failure="stale-legacy-source", prevention="newer fix")
        before = learning.list_verified_lessons("owner/repo")
        self.assertEqual(before[0]["prevention"], "newer fix")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            learning.migrate_json(self.json_path)
        self.assertEqual(learning.list_verified_lessons("owner/repo"), before)
        with open(self.json_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["lessons"], [older])

    def test_rerunning_identical_json_does_not_rewrite_destination(self):
        record = self.add(failure="identical-source")
        self.write_json([record])
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TRIGGER reject_update BEFORE UPDATE ON lessons "
                "BEGIN SELECT RAISE(FAIL, 'unexpected rewrite'); END"
            )
        result = learning.migrate_json(self.json_path)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)

    def test_migration_rollback_on_sql_failure(self):
        original = self.add()
        import sqlite3
        with sqlite3.connect(self.path) as connection:
            connection.execute("""CREATE TRIGGER reject_lesson BEFORE INSERT ON lessons
                               BEGIN SELECT RAISE(FAIL, 'test insert failure'); END""")
        self.write_json([{**original, "id": "new-id", "failure": "new failure"}])
        with self.assertRaises(learning.LessonStorageError):
            learning.migrate_json(self.json_path)
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)

    def test_read_and_write_error_surfaces(self):
        self.add()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE lessons")
        with self.assertRaises(learning.LessonStorageError):
            learning.list_verified_lessons("owner/repo")
        with self.assertRaises(learning.LessonStorageError):
            self.add()

    def test_long_discord_empty_and_error(self):
        repo = "a" * 600 + "/" + "b" * 600
        reply = asyncio.run(discord_lessons.handle(None, {"action": "list", "repository": repo}))
        self.assertLessEqual(len(reply), 2000)
        self.assertTrue(reply.endswith("yet."))
        with patch.object(discord_lessons, "list_verified_lessons",
                          side_effect=learning.LessonStorageError("x" * 3000)):
            reply = asyncio.run(discord_lessons.handle(None, {"action": "list", "repository": repo}))
            self.assertLessEqual(len(reply), 2000)

    def test_discord_boundaries(self):
        for i in range(8):
            self.add(repo="a" * 100 + "/" + "b" * 100,
                     failure=str(i) + "x" * 799, cause="c" * 800,
                     prevention="p" * 800, evidence="e" * 800)
        repo = "a" * 100 + "/" + "b" * 100
        reply = asyncio.run(discord_lessons.handle(None, {"action": "list", "repository": repo}))
        self.assertLessEqual(len(reply), 2000)
        self.assertRegex(reply, r"Showing \d+ of \d+ lessons; \d+ omitted\.$")
        saved = asyncio.run(discord_lessons.handle(None, {"action": "add",
            "repository": repo, "failure": "f" * 800, "root_cause": "c" * 800,
            "prevention": "p" * 800, "evidence": "e" * 800}))
        self.assertLessEqual(len(saved), 2000)


if __name__ == "__main__":
    unittest.main()
