"""Inline Composio policy environment loading remains opt-in and fail-closed."""

import json
import unittest
from unittest.mock import patch

from composio_adapter import CapabilityPolicy


class InlinePolicyEnvironmentTests(unittest.TestCase):
    def test_inline_policy_is_loaded_when_file_is_unset(self):
        policy = {"autonomy": "observe", "connections": {"github": "ac_github"}, "actions": []}
        loaded = CapabilityPolicy.from_env({
            "MAX_COMPOSIO_ENABLED": "true",
            "MAX_COMPOSIO_POLICY": json.dumps(policy),
            "MAX_EXTERNAL_ACTIONS_DISABLED": "true",
        })
        self.assertEqual(loaded.connections, {"github": "ac_github"})
        self.assertTrue(loaded.emergency_off)
        self.assertEqual(loaded.actions, {})

    def test_inline_policy_is_used_when_configured_file_is_missing(self):
        policy = {"autonomy": "observe", "connections": {"github": "ac_github"}, "actions": []}
        with patch("composio_adapter.Path.read_text", side_effect=FileNotFoundError):
            loaded = CapabilityPolicy.from_env({
                "MAX_COMPOSIO_ENABLED": "true",
                "MAX_COMPOSIO_POLICY_FILE": "/missing/private-policy.json",
                "MAX_COMPOSIO_POLICY": json.dumps(policy),
            })
        self.assertEqual(loaded.connections, {"github": "ac_github"})

    def test_invalid_inline_policy_fails_closed_by_raising(self):
        with self.assertRaises(json.JSONDecodeError):
            CapabilityPolicy.from_env({
                "MAX_COMPOSIO_ENABLED": "true",
                "MAX_COMPOSIO_POLICY": "{not-json",
            })

    def test_disabled_integration_ignores_inline_policy(self):
        loaded = CapabilityPolicy.from_env({
            "MAX_COMPOSIO_ENABLED": "false",
            "MAX_COMPOSIO_POLICY": "{not-json",
        })
        self.assertTrue(loaded.emergency_off)
        self.assertEqual(loaded.actions, {})

    def test_existing_malformed_file_does_not_fall_back_to_inline(self):
        with patch("composio_adapter.Path.read_text", side_effect=json.JSONDecodeError("bad", "{", 0)):
            with self.assertRaises(json.JSONDecodeError):
                CapabilityPolicy.from_env({
                    "MAX_COMPOSIO_ENABLED": "true",
                    "MAX_COMPOSIO_POLICY_FILE": "/existing/but-malformed.json",
                    "MAX_COMPOSIO_POLICY": json.dumps({"autonomy": "observe", "actions": []}),
                })


if __name__ == "__main__":
    unittest.main()
