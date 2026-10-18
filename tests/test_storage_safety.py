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
                before = {}
                for path in (db, prefs, legacy, usage_file):
                    with open(path, "rb") as handle:
                        before[path] = handle.read()
                destination = os.path.join(self.temp.name, "backup")
                manifest = storage_admin.backup(destination)
                self.assertEqual(len([f for f in manifest["files"] if f["status"] == "backed_up"]), 4)
                self.assertEqual(manifest["files"][0]["lesson_count"], 1)
                for path, content in before.items():
                    with open(path, "rb") as handle:
                        self.assertEqual(handle.read(), content)
                with self.assertRaises(FileExistsError):
                    storage_admin.backup(destination)


    def test_backup_verification_rejects_modified_copy(self):
        with patch.dict(storage_admin.FILES, {
            "lessons_db": os.path.join(self.temp.name, "absent.sqlite3"),
            "legacy_lessons_json": os.path.join(self.temp.name, "absent.json"),
            "preferences": os.path.join(self.temp.name, "prefs.json"),
            "usage": os.path.join(self.temp.name, "absent-usage.json"),
        }):
            prefs = storage_admin.FILES["preferences"]
            with open(prefs, "w", encoding="utf-8") as handle:
                json.dump({"users": {}}, handle)
            destination = os.path.join(self.temp.name, "backup")
            manifest = storage_admin.backup(destination)
            self.assertEqual(storage_admin.verify_backup(destination), {
                "status": "verified", "files_verified": 1, "sources_missing": 3,
            })
            backed_up = next(row for row in manifest["files"] if row["name"] == "preferences")
            with open(os.path.join(destination, backed_up["backup"]), "ab") as handle:
                handle.write(b"tamper")
            with self.assertRaisesRegex(ValueError, "checksum"):
                storage_admin.verify_backup(destination)
            with open(prefs, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), {"users": {}})

    def test_backup_verification_checks_sqlite_integrity_and_counts(self):
        db = os.path.join(self.temp.name, "lessons.sqlite3")
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE lessons (id TEXT)")
            conn.execute("INSERT INTO lessons VALUES ('record-1')")
        with patch.dict(storage_admin.FILES, {
            "lessons_db": db,
            "legacy_lessons_json": os.path.join(self.temp.name, "missing.json"),
            "preferences": os.path.join(self.temp.name, "missing-prefs.json"),
            "usage": os.path.join(self.temp.name, "missing-usage.json"),
        }):
            destination = os.path.join(self.temp.name, "backup")
            manifest = storage_admin.backup(destination)
            self.assertEqual(storage_admin.verify_backup(destination)["files_verified"], 1)
            row = next(item for item in manifest["files"] if item["name"] == "lessons_db")
            self.assertEqual(row["lesson_count"], 1)
            self.assertEqual(row["sha256"], storage_admin._sha256(
                storage_admin.Path(destination) / row["backup"]))

    def test_backup_verification_rejects_unsafe_or_incomplete_manifest(self):
        with patch.dict(storage_admin.FILES, {
            "lessons_db": os.path.join(self.temp.name, "missing.sqlite3"),
            "legacy_lessons_json": os.path.join(self.temp.name, "missing.json"),
            "preferences": os.path.join(self.temp.name, "missing-prefs.json"),
            "usage": os.path.join(self.temp.name, "missing-usage.json"),
        }):
            destination = os.path.join(self.temp.name, "backup")
            storage_admin.backup(destination)
            path = os.path.join(destination, "manifest.json")
            with open(path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            manifest["files"][0]["status"] = "backed_up"
            manifest["files"][0]["backup"] = "../secret.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)
            with self.assertRaisesRegex(ValueError, "filename"):
                storage_admin.verify_backup(destination)
            manifest["files"].pop()
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)
            with self.assertRaises(ValueError):
                storage_admin.verify_backup(destination)


if __name__ == "__main__":
    unittest.main()
