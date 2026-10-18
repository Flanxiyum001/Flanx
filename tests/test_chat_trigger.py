import unittest

from main import _strip_name_trigger, chat_intent


class MaxChatTriggerTests(unittest.TestCase):
    def test_greetings_addressing_max_are_recognized(self):
        for message in ("hi max", "hey max", "hello max", "yo max"):
            with self.subTest(message=message):
                remainder = _strip_name_trigger(message)
                self.assertIsNotNone(remainder)
                intent, _ = chat_intent(remainder)
                self.assertEqual(intent, "greeting")

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

    def test_explicit_file_repair_still_routes_to_fix(self):
        intent, payload = chat_intent(
            "fix src/app.py in owner/repo: handle HTTP 405 errors"
        )
        self.assertEqual(intent, "fix")
        self.assertEqual(payload["repo"], "owner/repo")
        self.assertEqual(payload["file"], "src/app.py")


if __name__ == "__main__":
    unittest.main()
