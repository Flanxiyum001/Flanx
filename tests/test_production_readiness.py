"""Offline production-readiness regression checks (no provider/GitHub API calls)."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import main
import free_sandbox
import model_router
from authz import github_user_allowed, GITHUB_ACCESS_DENIED
from integrations import repos, lessons
from provider_errors import configured_model_issue, safe_provider_error
from repo_review import (
    contains_potential_secret, extract_repository, inspect_repository,
    normalize_repository, validate_target_path,
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

    def test_literal_secret_detection_avoids_env_reference_false_positive(self):
        self.assertFalse(contains_potential_secret(
            "API_KEY = os.getenv('AI_API_KEY')"
        ))
        self.assertFalse(contains_potential_secret("print('hello')"))
        self.assertTrue(contains_potential_secret(
            'API_KEY = "a-very-long-embedded-secret-value"'
        ))

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

    def test_obvious_secrets_are_redacted_from_public_review_context(self):
        repo = FakeRepository()
        repo.source["src/app.py"] = (
            'API_KEY = "SENSITIVE-DEMO-SECRET"\n'
            'AUTH_TOKEN = "another-secret-value"\n'
            "print('hello')\n"
        )
        client = SimpleNamespace(get_repo=lambda name: repo)
        evidence = inspect_repository(client, "owner/repo")
        self.assertNotIn("SENSITIVE-DEMO-SECRET", evidence["context"])
        self.assertNotIn("another-secret-value", evidence["context"])
        self.assertIn("[REDACTED]", evidence["context"])

    def test_private_repo_does_not_read_or_forward_code(self):
        repo = FakeRepository()
        repo.private = True
        client = SimpleNamespace(get_repo=lambda name: repo)
        evidence = inspect_repository(client, "owner/repo")
        self.assertTrue(evidence["private"])
        self.assertFalse(repo.fetched)
        self.assertEqual(evidence["files"], [])
        self.assertNotIn("def run():", evidence["context"])
        # Private repository paths must not be enumerated either.
        with patch.object(repo, "get_git_tree", side_effect=AssertionError("tree fetched")):
            again = inspect_repository(client, "owner/repo")
        self.assertEqual(again["files"], [])


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


class GitHubPatchWorkflowTests(unittest.TestCase):
    def test_stale_source_blocks_branch_creation_and_update(self):
        remote = MagicMock()
        remote.default_branch = "main"
        remote.get_contents.return_value = SimpleNamespace(sha="newer-sha")
        with self.assertRaisesRegex(ValueError, "source file changed"):
            main.open_pull_request(
                remote, "src/app.py", "print(1)", "fix", "smoke check",
                expected_file_sha="older-sha",
            )
        remote.create_git_ref.assert_not_called()
        remote.update_file.assert_not_called()

    def test_patch_update_only_occurs_on_new_feature_branch(self):
        remote = MagicMock()
        remote.default_branch = "main"
        remote.get_branch.return_value = SimpleNamespace(
            commit=SimpleNamespace(sha="base-commit"))
        remote.get_contents.return_value = SimpleNamespace(sha="same-sha")
        remote.create_pull.return_value = SimpleNamespace(
            html_url="https://github.com/owner/repo/pull/9")
        main.open_pull_request(
            remote, "src/app.py", "print(2)", "fix", "sandbox-ok",
            expected_file_sha="same-sha",
        )
        self.assertEqual(remote.create_git_ref.call_count, 1)
        branch = remote.update_file.call_args.kwargs["branch"]
        self.assertNotEqual(branch, "main")
        self.assertEqual(remote.create_pull.call_args.kwargs["head"], branch)
        self.assertEqual(
            remote.update_file.call_args.kwargs["sha"], "same-sha"
        )


class GitHubAccessControlTests(unittest.TestCase):
    def test_missing_or_malformed_allowlist_fails_closed(self):
        self.assertFalse(github_user_allowed(1234, {}))
        self.assertFalse(github_user_allowed(1234, {"MAX_GITHUB_ALLOWED_USER_IDS": "invalid"}))
        self.assertFalse(github_user_allowed(-1, {"MAX_GITHUB_ALLOWED_USER_IDS": "-1"}))
        self.assertFalse(github_user_allowed(1234, {"MAX_GITHUB_ALLOWED_USER_IDS": "12345"}))
        self.assertTrue(github_user_allowed(1234, {"MAX_GITHUB_ALLOWED_USER_IDS": "9, 1234,5678"}))

    def test_unknown_discord_actor_cannot_enumerate_github(self):
        event = SimpleNamespace(author_id=8)
        with patch.dict("os.environ", {"MAX_GITHUB_ALLOWED_USER_IDS": "7"}):
            with patch.object(repos, "_github") as github:
                result = asyncio.run(repos.handle(event, {"action": "list", "page": 1}))
        self.assertEqual(result, GITHUB_ACCESS_DENIED)
        github.assert_not_called()

    def test_untrusted_actor_cannot_read_or_poison_verified_lessons(self):
        event = SimpleNamespace(author_id=8)
        with patch.dict("os.environ", {"MAX_GITHUB_ALLOWED_USER_IDS": "7"}):
            with patch.object(lessons, "add_verified_lesson") as write:
                with patch.object(lessons, "list_verified_lessons") as read:
                    denied_read = asyncio.run(lessons.handle(
                        event, {"action": "list", "repository": "owner/repo"}))
                    denied_write = asyncio.run(lessons.handle(
                        event, {"action": "add", "repository": "owner/repo"}))
        self.assertEqual(denied_read, GITHUB_ACCESS_DENIED)
        self.assertEqual(denied_write, GITHUB_ACCESS_DENIED)
        read.assert_not_called()
        write.assert_not_called()

    def test_memory_helper_never_writes_custom_default_branch(self):
        client = MagicMock()
        remote = client.get_repo.return_value
        remote.default_branch = "production"
        with patch.object(free_sandbox, "_client", return_value=client):
            result = free_sandbox.update_flanx_brain(
                "owner/repo", "never commit without approval", ref="production"
            )
        self.assertFalse(result)
        remote.update_file.assert_not_called()
        remote.create_file.assert_not_called()

    def test_public_github_brain_redacts_credential_assignments(self):
        client = MagicMock()
        remote = client.get_repo.return_value
        remote.private = False
        remote.get_contents.return_value.decoded_content = (
            b'API_KEY = "synthetic-secret-value-123456"'
        )
        with patch.object(free_sandbox, "_client", return_value=client):
            result = free_sandbox.read_flanx_brain("owner/repo")
        self.assertIn("[REDACTED]", result)
        self.assertNotIn("synthetic-secret-value", result)

    def test_private_github_brain_does_not_read_source(self):
        client = MagicMock()
        repository = client.get_repo.return_value
        repository.private = True
        with patch.object(free_sandbox, "_client", return_value=client):
            result = free_sandbox.read_flanx_brain("owner/private")
        self.assertIn("Private repository memory", result)
        repository.get_contents.assert_not_called()

    def test_private_repo_inspection_never_fetches_source_contents(self):
        repo = SimpleNamespace(full_name="owner/private", private=True)
        client = SimpleNamespace(get_repo=lambda identifier: repo)
        with patch.object(repos, "_github", return_value=client):
            result = repos._inspect("owner/private")
        self.assertIn("(private)", result)
        self.assertNotIn("Root entries", result)


class RuntimeSelectionTests(unittest.TestCase):
    def test_compilers_are_documented_onlinecompiler_ids(self):
        expected = {
            "example.py": "python-3.14",
            "src/app.ts": "typescript-deno",
            "src/app.js": "typescript-deno",
            "main.go": "go-1.26",
            "app.rb": "ruby-4.0",
        }
        for filename, compiler in expected.items():
            with self.subTest(filename=filename):
                self.assertEqual(main.compiler_for_target(filename), compiler)
        self.assertIsNone(main.compiler_for_target("script.sh"))

    def test_local_sandbox_usage_never_claims_live_quota(self):
        import usage
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as folder:
            with patch.object(usage, "USAGE_FILE", str(Path(folder) / "usage.json")):
                with patch.object(usage, "SANDBOX_MONTHLY_QUOTA", 0):
                    text = usage.format_usage()
                    self.assertIn("live provider allowance not checked", text)
                with patch.object(usage, "SANDBOX_MONTHLY_QUOTA", 100):
                    text = usage.format_usage()
                    self.assertIn("estimated remaining", text)
                    self.assertNotIn("left on the free plan", text)


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

    async def test_credential_bearing_source_blocks_external_processing(self):
        context = SimpleNamespace(send=AsyncMock())
        repository = MagicMock()
        repository.private = False
        repository.default_branch = "main"
        repository.get_contents.return_value = SimpleNamespace(
            sha="source-sha",
            decoded_content=b'API_KEY = "this-is-a-fake-sensitive-credential"'
        )
        with patch.object(main.gh, "get_repo", return_value=repository), \
             patch.object(main, "run_code") as sandbox, \
             patch.object(main, "generate_patch") as model:
            await main.fix_pipeline(context, "owner/repo", "src/app.py", "fix it")
        sandbox.assert_not_called()
        model.assert_not_called()
        self.assertIn("literal credential", context.send.await_args.args[0])

    async def test_private_repair_does_not_fetch_source_or_call_external_services(self):
        ctx = SimpleNamespace(send=AsyncMock())
        private_repo = MagicMock()
        private_repo.private = True
        with patch.object(main.gh, "get_repo", return_value=private_repo), \
             patch.object(main, "run_code") as sandbox, \
             patch.object(main, "generate_patch") as model:
            await main.fix_pipeline(ctx, "owner/private", "src/app.py", "fix it")
        private_repo.get_contents.assert_not_called()
        sandbox.assert_not_called()
        model.assert_not_called()
        self.assertIn("Private-repository", ctx.send.await_args.args[0])

    async def test_invalid_file_never_reaches_github(self):
        ctx = SimpleNamespace(send=AsyncMock())
        with patch.object(main, "gh") as fake_github:
            await main.fix_pipeline(ctx, "owner/repo", "../secrets.py", "fix it")
        fake_github.get_repo.assert_not_called()
        self.assertTrue(ctx.send.await_count)

    async def test_prefix_commands_require_operator(self):
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=7),
            send=AsyncMock(),
            typing=MagicMock(),
        )
        with patch.object(main, "github_user_allowed", return_value=False):
            await main.flanx_fix.callback(
                ctx, "owner/repo", "src/app.py", diagnostic_goal="fix"
            )
            await main.update_repo.callback(
                ctx, "owner/repo", "src/app.py", instruction="fix"
            )
            await main.flanx_brain.callback(ctx, "owner/repo")
        self.assertEqual(ctx.send.await_count, 3)
        for call in ctx.send.await_args_list:
            self.assertEqual(call.args[0], GITHUB_ACCESS_DENIED)

    async def test_no_ai_base_url_fails_without_network(self):
        with patch.object(main, "ai_client", None):
            response = await main.generate_assistant_reply("hi")
        self.assertIn("AI_BASE_URL", response)


    async def test_malformed_usage_metadata_does_not_discard_successful_reply(self):
        response = SimpleNamespace(
            model="auto",
            choices=[SimpleNamespace(message=SimpleNamespace(content="AI answer is intact"))],
            usage={"prompt_tokens": "not-a-number"},
        )
        fake = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=response))
        ))
        with patch.object(main, "ai_client", fake), \
             patch.object(main, "record_llm_usage", side_effect=ValueError("sensitive data")):
            answer = await main.generate_assistant_reply("hello")
        self.assertEqual(answer, "AI answer is intact")

    async def test_malformed_gateway_chat_is_reported_not_crashed(self):
        fake = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(
                return_value=SimpleNamespace(choices=[], usage=None)))),
        )
        with patch.object(main, "ai_client", fake):
            reply = await main.generate_assistant_reply("hello")
        self.assertIn("malformed", reply.lower())

    async def test_malformed_gateway_review_is_not_claimed_complete(self):
        evidence = {
            "name": "owner/repo", "private": False, "branch": "main",
            "files": ["README.md"], "truncated": False, "context": "README data"
        }
        fake = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(
                return_value=SimpleNamespace(choices=[], usage=None)))),
        )
        with patch.object(main, "ai_client", fake):
            reply = await main.generate_review_reply(evidence, "review")
        self.assertIn("malformed", reply.lower())


    async def test_long_chat_reply_uses_bounded_discord_messages(self):
        channel = SimpleNamespace(send=AsyncMock(), typing=MagicMock())
        message = SimpleNamespace(
            author=SimpleNamespace(id=7, bot=False),
            content="max explain long example",
            mentions=[], channel=channel,
        )
        answer = ("An example sentence with multiple words.\n" * 180)
        with patch.object(main, "get_pref", return_value=None), \
             patch.object(main, "generate_assistant_reply", new_callable=AsyncMock, return_value=answer), \
             patch.object(main.bot, "process_commands", new_callable=AsyncMock) as commands:
            await main.on_message(message)
        self.assertGreater(channel.send.await_count, 1)
        self.assertTrue(all(
            len(call.args[0]) <= 1900
            for call in channel.send.await_args_list
        ))
        commands.assert_awaited_once_with(message)

    async def test_private_review_returns_metadata_only(self):
        evidence = {
            "name": "owner/repo", "private": True, "branch": "main",
            "files": [], "truncated": False, "context": "metadata only"
        }
        reply = await main.generate_review_reply(evidence, "review")
        self.assertIn("did not send", reply)


class DiscordFormattingTests(unittest.TestCase):
    def test_multichunk_prose_never_exceeds_limit(self):
        chunks = main.discord_chunks("paragraph\n" * 1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 1900 for chunk in chunks))

    def test_long_code_fence_is_balanced_in_every_message(self):
        fence = chr(96) * 3
        code = fence + "python\n" + ("print('safe')\n" * 300) + fence
        chunks = main.discord_chunks(code)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1900 for chunk in chunks))
        self.assertTrue(all(chunk.count(fence) % 2 == 0 for chunk in chunks))



if __name__ == "__main__":
    unittest.main()
