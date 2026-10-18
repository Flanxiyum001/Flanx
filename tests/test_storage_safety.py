"""Tests for non-destructive usage persistence and storage backup helpers."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from io import StringIO

import usage
import storage_admin
import learning


class UsageStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "usage.json")
        self.patch = patch.object(usage, "USAGE_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def _add_database_record(self, path, failure="safe-test"):
        with patch.object(learning, "LESSONS_DB", path):
            return learning.add_verified_lesson(
                "owner/repo", failure, "root cause", "prevention", "test evidence"
            )

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

    def test_nested_usage_corruption_fails_closed_without_reset(self):
        for payload in (
            {"llm": {"calls": "many"}, "sandbox": {}},
            {"llm": {"cost_usd": "private"}, "sandbox": {}},
            {"llm": {"by_model": {"model": None}}, "sandbox": {}},
            {"llm": {}, "sandbox": {"2026-10": {"runs": -1}}},
        ):
            with self.subTest(payload=payload):
                original = json.dumps(payload).encode("utf-8")
                with open(self.path, "wb") as handle:
                    handle.write(original)
                with self.assertRaises(usage.UsageStorageError):
                    usage.record_llm_usage("test", {"prompt_tokens": 1})
                with open(self.path, "rb") as handle:
                    self.assertEqual(handle.read(), original)

    def test_interrupted_usage_write_preserves_original(self):
        usage.record_llm_usage("model", {"prompt_tokens": 1})
        with open(self.path, "rb") as handle:
            original = handle.read()
        with patch.object(usage.os, "replace", side_effect=PermissionError("denied")):
            with self.assertRaises(usage.UsageStorageError):
                usage.record_llm_usage("model", {"prompt_tokens": 2})
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_backup_json_and_sqlite_without_modifying_sources(self):
        db = os.path.join(self.temp.name, "lessons.sqlite3")
        prefs = os.path.join(self.temp.name, "prefs.json")
        legacy = os.path.join(self.temp.name, "legacy.json")
        usage_file = os.path.join(self.temp.name, "usage.json")
        self._add_database_record(db)
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
            self.assertEqual(backed_up["record_count"], 0)
            with open(os.path.join(destination, backed_up["backup"]), "ab") as handle:
                handle.write(b"tamper")
            with self.assertRaisesRegex(ValueError, "checksum"):
                storage_admin.verify_backup(destination)
            with open(prefs, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), {"users": {}})

    def test_backup_verification_checks_sqlite_integrity_and_counts(self):
        db = os.path.join(self.temp.name, "lessons.sqlite3")
        self._add_database_record(db, failure="record-1")
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
            prefs_path = storage_admin.FILES["preferences"]
            with open(prefs_path, "w", encoding="utf-8") as handle:
                json.dump({"users": {}}, handle)
            destination = os.path.join(self.temp.name, "backup")
            storage_admin.backup(destination)
            path = os.path.join(destination, "manifest.json")
            with open(path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            manifest["files"][0]["status"] = "backed_up"
            manifest["files"][0]["validation"] = "valid"
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


    def test_usage_storage_failure_does_not_abort_successful_reply(self):
        import main
        def failed_record(*args):
            raise usage.UsageStorageError("private diagnostic")
        with patch("sys.stdout", new_callable=StringIO) as output:
            self.assertIsNone(main._record_usage_safely(failed_record, "test"))
        self.assertIn("Usage counters unavailable", output.getvalue())
        self.assertNotIn("private diagnostic", output.getvalue())


    def test_json_manifest_count_is_verified(self):
        preferences = os.path.join(self.temp.name, "prefs.json")
        with open(preferences, "w", encoding="utf-8") as handle:
            json.dump({"users": {"101": {"prefs": {}, "notes": []}}}, handle)
        with patch.dict(storage_admin.FILES, {
            "lessons_db": os.path.join(self.temp.name, "missing.sqlite3"),
            "legacy_lessons_json": os.path.join(self.temp.name, "missing.json"),
            "preferences": preferences,
            "usage": os.path.join(self.temp.name, "missing-usage.json"),
        }):
            backup_dir = os.path.join(self.temp.name, "backup")
            manifest = storage_admin.backup(backup_dir)
            entry = next(row for row in manifest["files"] if row["name"] == "preferences")
            self.assertEqual(entry["record_count"], 1)
            entry["record_count"] = 2
            with open(os.path.join(backup_dir, "manifest.json"), "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)
            with self.assertRaisesRegex(ValueError, "validation/count mismatch"):
                storage_admin.verify_backup(backup_dir)

    def test_all_missing_sources_are_not_a_verified_backup(self):
        with patch.dict(storage_admin.FILES, {
            name: os.path.join(self.temp.name, name + ".missing")
            for name in storage_admin.FILES
        }):
            destination = os.path.join(self.temp.name, "empty-backup")
            with self.assertRaisesRegex(ValueError, "no source data"):
                storage_admin.backup(destination)
            self.assertTrue(os.path.isdir(destination))


    def test_inventory_distinguishes_missing_valid_empty_and_populated(self):
        db = os.path.join(self.temp.name, "lessons.sqlite3")
        prefs = os.path.join(self.temp.name, "prefs.json")
        legacy = os.path.join(self.temp.name, "legacy.json")
        with open(prefs, "w", encoding="utf-8") as handle:
            json.dump({"users": {}}, handle)
        with open(legacy, "w", encoding="utf-8") as handle:
            json.dump({"lessons": []}, handle)
        with patch.dict(storage_admin.FILES, {
            "lessons_db": db, "preferences": prefs,
            "legacy_lessons_json": legacy,
            "usage": os.path.join(self.temp.name, "missing-usage.json"),
        }):
            rows = {row["name"]: row for row in storage_admin.inventory()}
            self.assertEqual(rows["lessons_db"]["status"], "missing")
            self.assertEqual(rows["lessons_db"]["history"], "unknown")
            self.assertFalse(rows["lessons_db"]["exists"])
            self.assertEqual(rows["preferences"]["status"], "valid_empty")
            self.assertEqual(rows["legacy_lessons_json"]["status"], "valid_empty")
            self.assertEqual(rows["preferences"]["record_count"], 0)
            self.assertEqual(len(rows["preferences"]["sha256"]), 64)
            self._add_database_record(db)
            rows = {row["name"]: row for row in storage_admin.inventory()}
            self.assertEqual(rows["lessons_db"]["status"], "valid_records")
            self.assertEqual(rows["lessons_db"]["record_count"], 1)
            self.assertEqual(rows["lessons_db"]["sqlite_integrity"], "ok")

    def test_expected_missing_store_blocks_backup_without_creating_directory(self):
        db = os.path.join(self.temp.name, "lessons.sqlite3")
        with patch.dict(storage_admin.FILES, {
            name: os.path.join(self.temp.name, name + ".missing")
            for name in storage_admin.FILES
        }):
            storage_admin.FILES["lessons_db"] = db
            rows = {row["name"]: row for row in storage_admin.inventory(
                expected_existing=["lessons_db"]
            )}
            self.assertEqual(rows["lessons_db"]["status"], "missing_expected")
            destination = os.path.join(self.temp.name, "never-created")
            with self.assertRaisesRegex(ValueError, "missing_expected"):
                storage_admin.backup(destination, expected_existing=["lessons_db"])
            self.assertFalse(os.path.exists(destination))

    def test_inventory_corruption_invalid_path_and_inaccessible_file(self):
        prefs = os.path.join(self.temp.name, "prefs.json")
        with open(prefs, "wb") as handle:
            handle.write(b'{"users":')
        corrupt = storage_admin._inspect("preferences", prefs)
        self.assertEqual(corrupt["status"], "corrupt")
        self.assertTrue(corrupt["exists"])
        self.assertEqual(len(corrupt["sha256"]), 64)
        self.assertTrue(corrupt["readable"])
        directory = storage_admin._inspect("lessons_db", self.temp.name)
        self.assertEqual(directory["status"], "invalid_path")
        invalid_parent = storage_admin._inspect(
            "lessons_db", os.path.join(self.temp.name, "nonexistent", "lessons.sqlite3")
        )
        self.assertEqual(invalid_parent["status"], "invalid_path")
        original = storage_admin.Path.lstat

        def unreadable(path, *args, **kwargs):
            if str(path) == prefs:
                raise PermissionError("simulated denial")
            return original(path, *args, **kwargs)

        with patch.object(storage_admin.Path, "lstat", unreadable):
            self.assertEqual(
                storage_admin._inspect("preferences", prefs)["status"], "inaccessible"
            )
        with patch.dict(storage_admin.FILES, {
            "lessons_db": os.path.join(self.temp.name, "no.sqlite3"),
            "legacy_lessons_json": os.path.join(self.temp.name, "no-legacy.json"),
            "preferences": prefs,
            "usage": os.path.join(self.temp.name, "no-usage.json"),
        }):
            destination = os.path.join(self.temp.name, "reject-backup")
            with self.assertRaisesRegex(ValueError, "preflight failed"):
                storage_admin.backup(destination)
            self.assertFalse(os.path.exists(destination))
            with open(prefs, "rb") as handle:
                self.assertEqual(handle.read(), b'{"users":')

    def test_sqlite_corrupt_and_wrong_schema_fail_closed(self):
        bad = os.path.join(self.temp.name, "bad.sqlite3")
        with open(bad, "wb") as handle:
            handle.write(b"not a sqlite file")
        self.assertEqual(storage_admin._inspect("lessons_db", bad)["status"], "corrupt")
        os.unlink(bad)
        with sqlite3.connect(bad) as conn:
            conn.execute("CREATE TABLE lessons (id TEXT)")
        self.assertEqual(storage_admin._inspect("lessons_db", bad)["status"], "corrupt")
        with sqlite3.connect(bad) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0], 0)

    def test_wal_commits_are_included_in_sqlite_online_backup(self):
        db = os.path.join(self.temp.name, "wal.sqlite3")
        with sqlite3.connect(db) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(learning._SCHEMA)
            connection.execute(
                "INSERT INTO lessons (id, repository, failure, root_cause, prevention,"
                " evidence, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("id-wal", "owner/repo", "wal", "root", "prevent", "proof",
                 "verified", "2026-10-18", "2026-10-18"),
            )
            connection.commit()
            with patch.dict(storage_admin.FILES, {
                "lessons_db": db,
                "legacy_lessons_json": os.path.join(self.temp.name, "absent.json"),
                "preferences": os.path.join(self.temp.name, "absent-prefs.json"),
                "usage": os.path.join(self.temp.name, "absent-usage.json"),
            }):
                backup_dir = os.path.join(self.temp.name, "wal-backup")
                manifest = storage_admin.backup(backup_dir)
                self.assertEqual(manifest["files"][0]["lesson_count"], 1)
                self.assertEqual(storage_admin.verify_backup(backup_dir)["files_verified"], 1)
                with sqlite3.connect(os.path.join(backup_dir, "lessons_db.sqlite3")) as saved:
                    self.assertEqual(saved.execute("SELECT failure FROM lessons").fetchone()[0], "wal")

    def test_failed_copy_preserves_partial_directory_and_incomplete_manifest(self):
        source = os.path.join(self.temp.name, "prefs.json")
        with open(source, "w", encoding="utf-8") as handle:
            json.dump({"users": {}}, handle)
        with patch.dict(storage_admin.FILES, {
            "lessons_db": os.path.join(self.temp.name, "absent.sqlite3"),
            "legacy_lessons_json": os.path.join(self.temp.name, "absent.json"),
            "preferences": source,
            "usage": os.path.join(self.temp.name, "absent-usage.json"),
        }):
            target = os.path.join(self.temp.name, "partial")
            with patch.object(storage_admin.shutil, "copy2", side_effect=OSError("interrupted")):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    storage_admin.backup(target)
            self.assertTrue(os.path.isdir(target))
            with open(os.path.join(target, "manifest.json"), encoding="utf-8") as handle:
                self.assertFalse(json.load(handle)["complete"])
            with self.assertRaisesRegex(ValueError, "incomplete"):
                storage_admin.verify_backup(target)
            with open(source, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), {"users": {}})

    def test_configured_environment_paths_override_defaults(self):
        prefs = os.path.join(self.temp.name, "persistent-data", "prefs.json")
        os.mkdir(os.path.dirname(prefs))
        with open(prefs, "w", encoding="utf-8") as handle:
            json.dump({"users": {}}, handle)
        with patch.dict(os.environ, {"FLANX_PREFS_FILE": prefs}):
            rows = {row["name"]: row for row in storage_admin.inventory()}
        self.assertEqual(rows["preferences"]["path"], prefs)
        self.assertEqual(rows["preferences"]["status"], "valid_empty")

    def test_valid_empty_databases_count_as_recoverable_files(self):
        db = os.path.join(self.temp.name, "empty.sqlite3")
        with sqlite3.connect(db) as conn:
            conn.execute(learning._SCHEMA)
        with patch.dict(storage_admin.FILES, {
            "lessons_db": db,
            "legacy_lessons_json": os.path.join(self.temp.name, "missing.json"),
            "preferences": os.path.join(self.temp.name, "missing-prefs.json"),
            "usage": os.path.join(self.temp.name, "missing-usage.json"),
        }):
            destination = os.path.join(self.temp.name, "backup")
            manifest = storage_admin.backup(destination)
            self.assertEqual(manifest["files"][0]["record_count"], 0)
            self.assertEqual(storage_admin.verify_backup(destination)["files_verified"], 1)

    def test_verify_backup_rejects_incomplete_manifest_and_extra_file(self):
        prefs = os.path.join(self.temp.name, "prefs.json")
        with open(prefs, "w", encoding="utf-8") as handle:
            json.dump({"users": {}}, handle)
        with patch.dict(storage_admin.FILES, {
            "lessons_db": os.path.join(self.temp.name, "absent.sqlite3"),
            "legacy_lessons_json": os.path.join(self.temp.name, "absent.json"),
            "preferences": prefs,
            "usage": os.path.join(self.temp.name, "absent-usage.json"),
        }):
            destination = os.path.join(self.temp.name, "backup")
            manifest = storage_admin.backup(destination)
            with open(os.path.join(destination, "unexpected.txt"), "w") as handle:
                handle.write("unrelated file")
            with self.assertRaisesRegex(ValueError, "Unexpected files"):
                storage_admin.verify_backup(destination)
            os.unlink(os.path.join(destination, "unexpected.txt"))
            manifest["complete"] = False
            with open(os.path.join(destination, "manifest.json"), "w") as handle:
                json.dump(manifest, handle)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                storage_admin.verify_backup(destination)

    def test_state_survives_fresh_process_with_configured_directory(self):
        persistent = os.path.join(self.temp.name, "simulated-volume")
        os.mkdir(persistent)
        state_env = dict(os.environ)
        state_env.update({
            "MAX_LESSONS_DB": os.path.join(persistent, "lessons.sqlite3"),
            "FLANX_PREFS_FILE": os.path.join(persistent, "preferences.json"),
            "FLANX_USAGE_FILE": os.path.join(persistent, "usage.json"),
        })
        first = (
            "import learning,prefs,usage; "
            "learning.add_verified_lesson('owner/repo','first','root','prevent','evidence'); "
            "prefs.set_pref(9,'timezone','UTC'); prefs.add_note(9,'note'); "
            "usage.record_llm_usage('model',{'prompt_tokens':2,'completion_tokens':3})"
        )
        second = (
            "import learning,prefs,usage; "
            "assert len(learning.list_verified_lessons('owner/repo'))==1; "
            "assert prefs.get_pref(9,'timezone')=='UTC'; "
            "assert prefs.list_notes(9)==['note']; "
            "assert usage.get_usage()['llm']['total_tokens']==5"
        )
        root = os.path.dirname(os.path.dirname(__file__))
        for script in (first, second):
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=root, env=state_env,
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
