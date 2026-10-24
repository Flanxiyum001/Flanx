import unittest

from main import DIAGNOSTIC_RE, _strip_name_trigger, chat_intent


class MaxChatTriggerTests(unittest.TestCase):
    def test_greetings_addressing_max_use_natural_chat(self):
        for message in ("hi max", "hey max", "hello max", "yo max"):
            with self.subTest(message=message):
                remainder = _strip_name_trigger(message)
                self.assertIsNotNone(remainder)
                intent, payload = chat_intent(remainder)
                self.assertEqual(intent, "general")
                self.assertTrue(payload["prompt"])

    def test_social_messages_use_the_model_instead_of_canned_replies(self):
        for message in ("how are you?", "thanks for helping", "who are you?"):
            with self.subTest(message=message):
                intent, payload = chat_intent(message)
                self.assertEqual(intent, "general")
                self.assertEqual(payload["prompt"], message)

    def test_max_prefix_strips_name_and_keeps_request(self):
        self.assertEqual(_strip_name_trigger("max what time is it"), "what time is it")

    def test_legacy_flanx_trigger_still_works(self):
        self.assertEqual(_strip_name_trigger("flanx stats"), "stats")
        self.assertEqual(_strip_name_trigger("hey flanx"), "hey")

    def test_sandbox_error_diagnostics_route_to_general_chat(self):
        messages = (
            "Matrix exception: Error code: 405",
            "the sandbox health check returned HTTP 405",
            "Max, investigate why the OnlineCompiler API is failing",
            "what does error code 405 mean?",
        )
        for message in messages:
            with self.subTest(message=message):
                intent, payload = chat_intent(message)
                self.assertEqual(intent, "general")
                self.assertEqual(payload["prompt"], message)

    def test_diagnostic_patterns_actually_match(self):
        messages = (
            "what does this error mean",
            "why is the sandbox broken",
            "diagnose the API issue",
            "investigate the connection failure",
            "troubleshoot status code 405",
            "Matrix exception: Error code: 405",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertIsNotNone(DIAGNOSTIC_RE.search(message))

    def test_diagnostics_with_repository_and_file_are_not_repairs(self):
        messages = (
            "Why did owner/repo src/app.py raise HTTP 405?",
            "explain the failure in src/app.py in owner/repo",
            "The API connection error mentions owner/repo and src/app.py.",
        )
        for message in messages:
            with self.subTest(message=message):
                intent, payload = chat_intent(message)
                self.assertEqual(intent, "general")
                self.assertEqual(payload["prompt"], message)

    def test_diagnostic_request_keeps_question_mark_and_whitespace_normalization(self):
        message = "  what does error code 405 mean?  "
        self.assertEqual(
            chat_intent(message),
            ("general", {"prompt": "what does error code 405 mean?"}),
        )

    def test_explicit_file_repair_still_routes_to_fix(self):
        intent, payload = chat_intent(
            "fix src/app.py in owner/repo: handle HTTP 405 errors"
        )
        self.assertEqual(intent, "fix")
        self.assertEqual(payload["repo"], "owner/repo")
        self.assertEqual(payload["file"], "src/app.py")


if __name__ == "__main__":
    unittest.main()
