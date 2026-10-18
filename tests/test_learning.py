"""Offline tests for the verified learning ledger."""
import json, os, tempfile, unittest
from unittest.mock import patch
import learning
class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "lessons.json")
        self.patch = patch.object(learning, "LESSONS_FILE", self.path)
        self.patch.start()
    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()
    def add(self, repo="owner/repo", failure="CI import error", cause="missing module", prevention="add module and test", evidence="CI passed after commit abc123"):
        return learning.add_verified_lesson(repo, failure, cause, prevention, evidence)
    def test_lesson_scoped_to_repository(self):
        self.add()
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)
        self.assertEqual(learning.list_verified_lessons("owner/other"), [])
    def test_evidence_required(self):
        with self.assertRaises(ValueError): self.add(evidence="")
    def test_owner_repo_format_required(self):
        with self.assertRaises(ValueError): self.add(repo="repo")
    def test_duplicate_updates_existing_lesson(self):
        first = self.add()
        second = self.add(repo="OWNER/REPO", failure="CI IMPORT ERROR", cause="MISSING MODULE", prevention="better fix", evidence="second test passed")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(learning.list_verified_lessons("owner/repo")), 1)
        self.assertEqual(learning.list_verified_lessons("owner/repo")[0]["prevention"], "better fix")
    def test_json_persistence(self):
        self.add()
        with open(self.path, encoding="utf-8") as handle: self.assertEqual(len(json.load(handle)["lessons"]), 1)
if __name__ == "__main__": unittest.main()
