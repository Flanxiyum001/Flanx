"""Offline safety tests for preference storage."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import prefs


class PreferenceStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "prefs.json")
        self.path_patch = patch.object(prefs, "PREFS_FILE", self.path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def write_bytes(self, value):
        with open(self.path, "wb") as handle:
            handle.write(value)

    def read_bytes(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def test_missing_file_returns_default_and_first_write_works(self):
        self.assertEqual(prefs.get_pref(10, "timezone", "UTC"), "UTC")
        self.assertEqual(prefs.set_pref(10, "timezone", "Asia/Kolkata")["users"]["10"]["prefs"]["timezone"], "Asia/Kolkata")

    def test_malformed_json_fails_closed_and_preserves_bytes(self):
        original = b'{"users": '
        self.write_bytes(original)
        with self.assertRaises(prefs.PreferenceStorageError) as raised:
            prefs.set_pref(10, "timezone", "UTC")
        self.assertIsInstance(raised.exception.__cause__, json.JSONDecodeError)
        self.assertEqual(self.read_bytes(), original)

    def test_invalid_top_level_structure_fails_closed_and_preserves_bytes(self):
        for original in (b"[]", b'{"users": []}', b'{"users": {}, "extra": null}'):
            with self.subTest(original=original):
                self.write_bytes(original)
                with self.assertRaises(prefs.PreferenceStorageError):
                    prefs.add_note(10, "do not lose this")
                self.assertEqual(self.read_bytes(), original)

    def test_io_failure_raises_and_does_not_write(self):
        original = b'{"users": {}}'
        self.write_bytes(original)
        real_open = open

        def failing_open(path, *args, **kwargs):
            if path == self.path and args and args[0] == "r":
                raise PermissionError("simulated read failure")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=failing_open):
            with self.assertRaises(prefs.PreferenceStorageError) as raised:
                prefs.set_pref(10, "timezone", "UTC")
        self.assertIsInstance(raised.exception.__cause__, PermissionError)
        self.assertEqual(self.read_bytes(), original)

    def test_normal_preferences_and_notes_round_trip(self):
        prefs.set_pref(42, "timezone", "UTC")
        self.assertEqual(prefs.get_pref(42, "timezone"), "UTC")
        self.assertEqual(prefs.add_note(42, "  remember this  "), 1)
        self.assertEqual(prefs.list_notes(42), ["remember this"])


    def test_interrupted_replace_preserves_original_and_cleans_temp(self):
        prefs.set_pref(42, "timezone", "UTC")
        original = self.read_bytes()
        with patch.object(prefs.os, "replace", side_effect=PermissionError("blocked")):
            with self.assertRaises(prefs.PreferenceStorageError):
                prefs.set_pref(42, "timezone", "Asia/Kolkata")
        self.assertEqual(self.read_bytes(), original)
        self.assertFalse(any(name.startswith(".flanx-prefs-")
                             for name in os.listdir(self.temp.name)))


if __name__ == "__main__":
    unittest.main()
