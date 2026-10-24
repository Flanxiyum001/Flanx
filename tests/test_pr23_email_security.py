"""Security regression tests for the PR #23 email integration."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from integrations import email


class EmailIntegrationSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_user_cannot_send_email(self):
        event = SimpleNamespace(author_id=123)
        with patch.object(email, "owner_access", return_value=SimpleNamespace(allowed=False)), \
             patch.object(email, "send_email") as send:
            result = await email.handle(event, {
                "recipient": "person@example.com",
                "subject": "hello",
                "body": "test",
            })
        self.assertIn("restricted", result.lower())
        send.assert_not_called()

    async def test_authorized_owner_can_send_valid_email(self):
        event = SimpleNamespace(author_id=42)
        with patch.object(email, "owner_access", return_value=SimpleNamespace(allowed=True)), \
             patch.object(email, "send_email", return_value=True) as send:
            result = await email.handle(event, {
                "recipient": "person@example.com",
                "subject": "hello",
                "body": "test",
            })
        self.assertIn("sent successfully", result.lower())
        send.assert_called_once_with("person@example.com", "hello", "test")

    async def test_subject_header_injection_is_rejected(self):
        event = SimpleNamespace(author_id=42)
        with patch.object(email, "owner_access", return_value=SimpleNamespace(allowed=True)), \
             patch.object(email, "send_email") as send:
            result = await email.handle(event, {
                "recipient": "person@example.com",
                "subject": "hello\r\nBcc: victim@example.com",
                "body": "test",
            })
        self.assertIn("invalid email subject", result.lower())
        send.assert_not_called()

    async def test_invalid_recipient_is_rejected(self):
        event = SimpleNamespace(author_id=42)
        with patch.object(email, "owner_access", return_value=SimpleNamespace(allowed=True)), \
             patch.object(email, "send_email") as send:
            result = await email.handle(event, {
                "recipient": "bad address",
                "subject": "hello",
                "body": "test",
            })
        self.assertIn("invalid recipient", result.lower())
        send.assert_not_called()

    async def test_oversized_body_is_rejected(self):
        event = SimpleNamespace(author_id=42)
        with patch.object(email, "owner_access", return_value=SimpleNamespace(allowed=True)), \
             patch.object(email, "send_email") as send:
            result = await email.handle(event, {
                "recipient": "person@example.com",
                "subject": "hello",
                "body": "x" * 20_001,
            })
        self.assertIn("too long", result.lower())
        send.assert_not_called()

    def test_email_parser_extracts_basic_message(self):
        parsed = email.match("max send email to person@example.com subject Hello body World")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["recipient"], "person@example.com")
        self.assertEqual(parsed["subject"], "Hello")
        self.assertEqual(parsed["body"], "World")


if __name__ == "__main__":
    unittest.main()
