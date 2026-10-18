"""Offline tests for GitHub repository workflow intent parsing."""
import unittest
from integrations import repos
class RepoIntentTests(unittest.TestCase):
    def test_repo_list_defaults_to_page_one(self): self.assertEqual(repos.match("repos"), {"action": "list", "page": 1})
    def test_repo_list_page_is_bounded(self): self.assertEqual(repos.match("repos 999"), {"action": "list", "page": 100})
    def test_repo_selection(self): self.assertEqual(repos.match("use octo/project"), {"action": "use", "repository": "octo/project"})
    def test_current_selection(self): self.assertEqual(repos.match("current repo"), {"action": "current"})
    def test_inspection(self): self.assertEqual(repos.match("inspect octo/project"), {"action": "inspect", "repository": "octo/project"})
    def test_unrelated_text_is_ignored(self): self.assertIsNone(repos.match("please explain python decorators"))
if __name__ == "__main__": unittest.main()

    def test_inspection_sections_have_bounded_content(self):
        import inspect
        source = inspect.getsource(repos._inspect)
        self.assertIn('("AGENTS.md", "Repository instructions", 500)', source)
        self.assertIn('("README.md", "README overview", 450)', source)
        self.assertIn('join(names[:30])[:300]', source)
