"""Regression tests for Discord lesson responses."""
import unittest
from unittest.mock import patch

from integrations import lessons


class LessonIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_listing_stays_below_discord_message_limit(self):
        record = {
            "failure": "F" * 800,
            "root_cause": "C" * 800,
            "prevention": "P" * 800,
            "evidence": "E" * 800,
        }
        with patch.object(lessons, "list_verified_lessons", return_value=[record] * 20), \
             patch.object(lessons, "github_user_allowed", return_value=True):
            response = await lessons.handle(
                type("Event", (), {"author_id": 7})(),
                {"action": "list", "repository": "owner/repo"},
            )
        self.assertLessEqual(len(response), 2000)
        self.assertIn("Showing", response)

    async def test_invalid_lesson_request_returns_usage(self):
        response = await lessons.handle(None, {"action": "invalid"})
        self.assertIn("owner/repo", response)
        self.assertIn("never include secrets", response.lower())


if __name__ == "__main__":
    unittest.main()
