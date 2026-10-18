"""Offline production-readiness regression checks (no provider/GitHub API calls)."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import main
import free_sandbox
import model_router
from provider_errors import configured_model_issue, safe_provider_error
from repo_review import (
    extract_repository, inspect_repository, normalize_repository,
    validate_target_path,
)


class RepositoryRoutingTests(unittest.TestCase):
    def test_github_url_normalization_with_git_suffix_and_trailing_slash(self):
        self.assertEqual(
            normalize_repository("https://github.com/Flanxiyum001/Axiom.git/"),
            "Flanxiyum001/Axiom",
        )
        self.assertEqual(
            normalize_repository("Flanxiyum001/Axiom.git"),
            "Flanxiyum001/Axiom",
        )

    def test_repository_url_discussion_is_not_file_repair(self):
        examples = (
            ("https://github.com/Flanxiyum001/Axiom", "repo_clarify"),
            ("https://github.com/Flanxiyum001/Axiom.git/", "repo_clarify"),
            ("review https://github.com/Flanxiyum001/Axiom", "review"),
            ("analyze this repository's architecture: owner/repo", "review"),
            ("review owner/repo", "review"),
            ("explain HTTP 405 in owner/repo", "general"),
        )
        for message, expected in examples:
            with self.subTest(message=message):
                intent, payload = main.chat_intent(message)
                self.assertEqual(intent, expected)
                if expected in {"repo_clarify", "review"}:
                    self.assertIn(payload["repo"], {"Flanxiyum001/Axiom", "owner/repo"})

    def test_review_needs_repository_but_does_not_become_repair(self):
        intent, payload = main.chat_intent("review this project")
        self.assertEqual(intent, "review")
        self.assertIsNone(payload["repo"])

    def test_explicit_file_repair_remains_repair(self):
        result = main.chat_intent(
            "fix src/app.py in owner/repo: handle invalid input"
        )
        self.assertEqual(result[0], "fix")
        self.assertEqual(result[1]["repo"], "owner/repo")
        self.assertEqual(result[1]["file"], "src/app.py")

    def test_file_and_repo_extraction_do_not_confuse_urls(self):
        url = "https://github.com/Flanxiyum001/Axiom.git/"
        self.assertEqual(extract_repository(url), "Flanxiyum001/Axiom")
        self.assertIsNone(main._extract_file(url))
        self.assertIsNone(extract_repository("src/app.py"))

    def test_target_path_rejects_escape_and_git_metadata(self):
        for bad in ("../src/app.py", "/etc/passwd", "src/../secret.py",
                    ".git/config", "src\\app.py", "https://bad.example/a.py",
                    "src//app.py", "src/\x00evil.py"):
            with self.subTest(path=bad):
                with self.assertRaises(ValueError):
                    validate_target_path(bad)
        self.assertEqual(validate_target_path("src/app.py"), "src/app.py")

    def test_invalid_github_repo_identifier(self):
        for bad in ("https://evil.example/a/b", "https://github.com/a/b/issues/1",
                    "https://github.com/a/../b", "src/../../secret",
                    "github.com/a/b/c"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    normalize_repository(bad)


class FakeRepository:
    full_name = "owner/repo"
    default_branch = "main"
    private = False
    description = "Public demonstration repository"

    def __init__(self):
        self.fetched = []
        self.source = {
            "README.md": "# Repo\nContains a CLI and sample docs.",
            "requirements.txt": "requests>=2",
            "src/app.py": "def run():\n    return 1",
            "tests/test_app.py": "def test_run():\n    assert True",
        }

    def get_git_tree(self, branch, recursive=False):
        assert branch == "main" and recursive is True
        return SimpleNamespace(
            tree=[SimpleNamespace(path=path, type="blob") for path in self.source],
            truncated=False,
        )

    def get_contents(self, path, ref):
        assert ref == "main"
        self.fetched.append(path)
        return SimpleNamespace(decoded_content=self.source[path].encode("utf-8"))


class ReadOnlyReviewTests(unittest.TestCase):
    def test_reads_relevant_real_paths_with_bounded_content(self):
        repo = FakeRepository()
        client = SimpleNamespace(get_repo=lambda name: repo)
        evidence = inspect_repository(client, "owner/repo")
        self.assertEqual(evidence["name"], "owner/repo")
        self.assertIn("README.md", evidence["files"])
        self.assertIn("tests/test_app.py", evidence["files"])
        self.assertIn("src/app.py", evidence["files"])
        self.assertIn("UNTRUSTED FILE CONTENT", evidence["context"])
        self.assertIn("src/app.py", evidence["context"])
        self.assertFalse(evidence["private"])

    def test_private_repo_does_not_read_or_forward_code(self):
        repo = FakeRepository()
        repo.private = True
        client = SimpleNamespace(get_repo=lambda name: repo)
        evidence = inspect_repository(client, "owner/repo")
        self.assertTrue(evidence["private"])
        self.assertFalse(repo.fetched)
        self.assertEqual(evidence["files"], [])
        self.assertNotIn("def run():", evidence["context"])


class ModelAndErrorTests(unittest.TestCase):
    def test_no_stale_model_default(self):
        self.assertEqual(model_router.configured_routes({})["coding"], "auto")
        self.assertIsNotNone(configured_model_issue("qwen-2.5-coder-32b"))
        self.assertIsNone(configured_model_issue("auto"))

    def test_safe_http_failure_messages_never_include_original_secret(self):
        secret = "should-never-appear-in-discord"
        class FakeStatusError(Exception):
            status_code = 401
        err = FakeStatusError(secret)
        self.assertIn("401", safe_provider_error(err))
        self.assertNotIn(secret, safe_provider_error(err))
        for status in (400, 403, 404, 405, 429, 500, 503):
            with self.subTest(status=status):
                err.status_code = status
                self.assertNotIn(secret, safe_provider_error(err))


class SandboxHTTPTests(unittest.TestCase):
    def setUp(self):
        self.key = patch.object(free_sandbox, "SANDBOX_API_KEY", "test-key-for-mocks")
        self.key.start()

    def tearDown(self):
        self.key.stop()

    def fake(self, status, data=None, body="UPSTREAM-SECRET"):
        response = MagicMock()
        response.status_code = status
        response.text = body
        response.json.return_value = data
        return response

    def test_documented_request_and_native_response(self):
        with patch.object(free_sandbox.requests, "post",
                          return_value=self.fake(200, {
                              "status": "success", "output": "2\n",
                              "error": "", "exit_code": 0, "time": "0.01",
                          })) as post:
            result = free_sandbox.run_code("print(1 + 1)")
        self.assertTrue(result.passed)
        self.assertEqual(result.stdout, "2\n")
        args, kwargs = post.call_args
        self.assertTrue(args[0].endswith("/api/run-code-sync/"))
        self.assertEqual(kwargs["json"], {
            "compiler": "python-3.14", "code": "print(1 + 1)", "input": "",
        })
        self.assertEqual(kwargs["headers"]["Authorization"], "test-key-for-mocks")
        self.assertNotIn("Bearer ", kwargs["headers"]["Authorization"])

    def test_sandbox_4xx_5xx_diagnostics_no_upstream_echo(self):
        for status in (400, 401, 403, 404, 405, 422, 429, 500, 503):
            with self.subTest(status=status):
                with patch.object(free_sandbox.requests, "post",
                                  return_value=self.fake(status)):
                    result = free_sandbox.run_code("print(1)")
                self.assertFalse(result.ok)
                self.assertIn(str(status), result.error)
                self.assertNotIn("UPSTREAM-SECRET", result.error)

    def test_invalid_json_and_malformed_result_never_pass(self):
        for payload in (
            [], {"error": "secret", "status": "error"},
            {"exit_code": True, "output": "ok"},
            {"exit_code": 0, "status": "error"},
            {"exit_code": 0, "status": "unknown"},
            {"exit_code": 0, "output": [1, 2]},
            {"exit_code": 0, "output": "2", "truncated": True},
        ):
            with self.subTest(payload=payload):
                with patch.object(free_sandbox.requests, "post",
                                  return_value=self.fake(200, payload)):
                    self.assertFalse(free_sandbox.run_code("print(2)").passed)
        fake = self.fake(200)
        fake.json.side_effect = ValueError("raw secret")
        with patch.object(free_sandbox.requests, "post", return_value=fake):
            self.assertFalse(free_sandbox.run_code("print(1)").ok)

    def test_missing_key_and_untrusted_input_do_not_send_request(self):
        with patch.object(free_sandbox, "SANDBOX_API_KEY", None), patch.object(
            free_sandbox.requests, "post"
        ) as post:
            self.assertFalse(free_sandbox.run_code("print(1)").ok)
            post.assert_not_called()
        with patch.object(free_sandbox.requests, "post") as post:
            self.assertFalse(free_sandbox.run_code("a" * 100001).ok)
            self.assertFalse(free_sandbox.run_code("print(1)\x00").ok)
            post.assert_not_called()


class AsyncSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_auth_diagnostic_does_not_leak_secret(self):
        class UntrustedStatus(Exception):
            status_code = 401
        fake = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(
                side_effect=UntrustedStatus("PRIVATE-KEY-AND-PROMPT"))),
        ))
        with patch.object(main, "ai_client", fake):
            reply = await main.generate_assistant_reply("hello")
        self.assertIn("401", reply)
        self.assertNotIn("PRIVATE-KEY", reply)

    async def test_invalid_file_never_reaches_github(self):
        ctx = SimpleNamespace(send=AsyncMock())
        with patch.object(main, "gh") as fake_github:
            await main.fix_pipeline(ctx, "owner/repo", "../secrets.py", "fix it")
        fake_github.get_repo.assert_not_called()
        self.assertTrue(ctx.send.await_count)

    async def test_private_review_returns_metadata_only(self):
        evidence = {
            "name": "owner/repo", "private": True, "branch": "main",
            "files": [], "truncated": False, "context": "metadata only"
        }
        reply = await main.generate_review_reply(evidence, "review")
        self.assertIn("did not send", reply)


if __name__ == "__main__":
    unittest.main()
