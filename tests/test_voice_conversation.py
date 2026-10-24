import unittest

from voice_conversation import FRAME_BYTES, WAKE_WORD_RE, _pcm_rms, extract_wake_command, normalize_transcript


class VoiceWakeWordTests(unittest.TestCase):
    def test_wake_word_extracts_inline_request(self):
        self.assertEqual(extract_wake_command("Hey Max, what is the weather?"), (True, "what is the weather?"))

    def test_wake_word_accepts_punctuation_and_case(self):
        self.assertEqual(extract_wake_command("HEY, MAX! Tell me a joke"), (True, "Tell me a joke"))
        self.assertEqual(extract_wake_command("hey...max"), (True, ""))

    def test_non_wake_transcript_is_not_a_command(self):
        self.assertEqual(extract_wake_command("Max is my friend"), (False, ""))
        self.assertIsNone(WAKE_WORD_RE.search("hey maximum"))

    def test_transcript_whitespace_is_normalized(self):
        self.assertEqual(normalize_transcript("  hello\n   Max  "), "hello Max")

    def test_pcm_rms_handles_empty_and_silence(self):
        self.assertEqual(_pcm_rms(b""), 0.0)
        self.assertEqual(_pcm_rms(bytes(FRAME_BYTES)), 0.0)

    def test_pcm_rms_detects_signal(self):
        pcm = (1000).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
        self.assertGreater(_pcm_rms(pcm), 900.0)


if __name__ == "__main__":
    unittest.main()
