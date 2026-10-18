"""Tests for non-destructive usage persistence and storage backup helpers."""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import usage
import storage_admin


class UsageStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "usage.json")
        self.patch = patch.object(usage, "USAGE_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_missing_file_defaults_and_first_write(self):
        self.assertEqual(usage.get_usage()["llm"]["calls"], 0)
        usage.record_llm_usage("test-model", {"prompt_tokens": 2, "completion_tokens": 3})
        self.assertEqual(usage.get_usage()["llm"]["total_tokens"], 5)

    def test_corrupt_json_is_preserved_and_raises(self):
        original = b'{"llm":'
        with open(self.path, "wb") as handle:
            handle.write(original)
        with self.assertRaises(usage.UsageStorageError):
            usage.record_llm_usage("test-model", {"prompt_tokens": 1})
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_invalid_structure_is_preserved_and_raises(self):
        original = b'{"llm": [], "sandbox": {}}'
        with open(self.path, "wb") as handle:
            handle.write(original)
        with self.assertRaises(usage.UsageStorageError):
            usage.get_usage()
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_backup_json_and_sqlite_without_modifying_sources(self):
        db = os.path.join(self.temp.name, "lessons.sqlite3")
        prefs = os.path.join(self.temp.name, "prefs.json")
        legacy = os.path.join(self.temp.name, "legacy.json")
        usage_file = os.path.join(self.temp.name, "usage.json")
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE lessons (id TEXT)")
            conn.execute("INSERT INTO lessons VALUES ('safe-test')")
        for path, payload in (
            (prefs, {"users": {}}),
            (legacy, {"lessons": []}),
            (usage_file, {"llm": {}, "sandbox": {}}),
        ):
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        with patch.dict(os.environ, {
            "MAX_LESSONS_DB": db, "MAX_LESSONS_FILE": legacy,
            "FLANX_PREFS_FILE": prefs, "FLANX_USAGE_FILE": usage_file,
        }):
            # FILES is evaluated at import, so patch the paths explicitly.
            with patch.dict(storage_admin.FILES, {
                "lessons_db": db, "legacy_lessons_json": legacy,
                "preferences": prefs, "usage": usage_file,
            }):
                before = {path: open(path, "rb").read() for path in (db, prefs, legacy, usage_file)}
                destination = os.path.join(self.temp.name, "backup")
                manifest = storage_admin.backup(destination)
                self.assertEqual(len([f for f in manifest["files"] if f["status"] == "backed_up"]), 4)
                self.assertEqual(manifest["files"][0]["lesson_count"], 1)
                for path, content in before.items():
                    with open(path, "rb") as handle:
                        self.assertEqual(handle.read(), content)
                with self.assertRaises(FileExistsError):
                    storage_admin.backup(destination)


if __name__ == "__main__":
    unittest.main()
