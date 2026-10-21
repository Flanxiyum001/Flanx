"""Offline tests for Max task-aware model routing."""

import unittest

from model_router import configured_routes, route_model


class ModelRouterTests(unittest.TestCase):
    def test_new_default_model_id(self):
        self.assertEqual(configured_routes({})["general"], "qwen3:8b")

    def test_explicit_override_wins(self):
        self.assertEqual(
            route_model("coding", override="manual-coder", fallback="legacy", environ={}),
            "manual-coder",
        )

    def test_task_specific_profile_wins_over_legacy_model(self):
        env = {"MAX_MODEL_CODING": "auto:coding", "AI_MODEL": "legacy-model"}
        self.assertEqual(route_model("coding", environ=env), "auto:coding")

    def test_legacy_model_remains_fallback(self):
        self.assertEqual(route_model("chat", environ={"AI_MODEL": "legacy-model"}), "legacy-model")

    def test_fallback_argument_precedes_legacy_environment(self):
        env = {"AI_MODEL": "legacy-model"}
        self.assertEqual(route_model("review", fallback="current-model", environ=env), "current-model")

    def test_whitespace_configuration_is_ignored(self):
        env = {"MAX_MODEL_CHAT": "   ", "AI_MODEL": "legacy-model"}
        self.assertEqual(route_model("chat", environ=env), "legacy-model")

    def test_unknown_task_uses_fallback(self):
        self.assertEqual(route_model("unknown", fallback="safe-model", environ={}), "safe-model")

    def test_routes_can_be_inspected_without_secrets(self):
        routes = configured_routes({"AI_MODEL": "legacy-model", "MAX_MODEL_CODING": "auto:coding"})
        self.assertEqual(routes["coding"], "auto:coding")
        self.assertEqual(routes["chat"], "legacy-model")
        self.assertEqual(set(routes), {"chat", "general", "research", "coding", "review"})


if __name__ == "__main__":
    unittest.main()
