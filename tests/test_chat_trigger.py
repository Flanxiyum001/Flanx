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


if __name__ == "__main__":
    unittest.main()
