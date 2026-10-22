"""Offline checks for bounded specialist roles and the copied workspace."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from specialists import (CodingWorkflow, ScopedWorkspace, SpecialistRegistry,
                         configured_role_models, review_diff, run_check, valid_plan)
from task_engine import Task


async def planner(payload):
    return {"steps": [{"action": "Edit the selected file", "outcome": "Requested behavior",
                       "verify": "Compile the result"}]}


class SpecialistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    async def test_registration_and_role_permissions(self):
        registry = SpecialistRegistry()
        with self.assertRaises(ValueError):
            registry.register("manager", planner)
        registry.register("planner", planner)
        with self.assertRaises(ValueError):
            registry.register("planner", planner)
        self.assertEqual((await registry.run("planner", {"goal": "Edit"}))["steps"][0]["action"],
                         "Edit the selected file")
        forged = Task.new("Run planner specialist", "specialist.planner",
                          permitted_tools={"workspace.edit"}, requested_role="planner")
        outcome = await registry.engine.run(forged, {"goal": "Edit"})
        self.assertEqual(outcome.task.error_code, "permission_denied")
        self.assertEqual(configured_role_models({"AI_MODEL": "legacy"})["coder"], "legacy")

    async def test_plan_validation_and_malformed_response(self):
        self.assertFalse(valid_plan({"steps": []}))
        self.assertFalse(valid_plan({"steps": [{"action": "a", "outcome": "b", "verify": "c"}] * 9}))

        async def invalid(_):
            return {"steps": [{"action": "Run code", "nested": []}]}

        workflow = CodingWorkflow(self.root, invalid, lambda _: None)
        report = await workflow.run("Change the value", ("app.py",))
        self.assertFalse(report.success)
        self.assertEqual((self.root / "app.py").read_text(), "value = 1\n")

    async def test_workspace_rejects_escape_and_out_of_scope_change(self):
        with self.assertRaises(ValueError):
            ScopedWorkspace(self.root, ("../outside.py",))
        with self.assertRaises(ValueError):
            ScopedWorkspace(self.root, ("missing.py",))
        workspace = ScopedWorkspace(self.root, ("app.py",))
        try:
            with self.assertRaises(ValueError):
                workspace.apply({"other.py": "unsafe"})
            workspace.apply({"app.py": "value = 2\n"})
            self.assertEqual((self.root / "app.py").read_text(), "value = 1\n")
            changed, diff = workspace.diff()
            self.assertEqual(changed, ("app.py",))
            self.assertIn("+value = 2", diff)
        finally:
            workspace.close()

    async def test_reviewer_inspects_real_diff_and_redacts_credential(self):
        workspace = ScopedWorkspace(self.root, ("app.py",))
        try:
            workspace.apply({"app.py": "API_KEY = 'private-value'\n"})
            _, diff = workspace.diff()
        finally:
            workspace.close()
        result = await review_diff({"diff": diff})
        self.assertEqual(result["findings"][0]["severity"], "high")
        self.assertNotIn("private-value", str(result["findings"]))
        execution = await review_diff({"diff": "--- a/app.py\n+++ b/app.py\n+subprocess.run(cmd)\n"})
        self.assertEqual(execution["findings"][0]["severity"], "medium")

    async def test_tester_exit_status_timeout_and_skip(self):
        workspace = ScopedWorkspace(self.root, ("app.py",))
        try:
            passed = await run_check({"workspace": str(workspace.root), "command": "compile"})
            self.assertEqual(passed["exit_code"], 0)
            self.assertFalse(passed["skipped"])
            (workspace.root / "app.py").write_text("def broken(:\n", encoding="utf-8")
            failed = await run_check({"workspace": str(workspace.root), "command": "compile"})
            self.assertNotEqual(failed["exit_code"], 0)
            (workspace.root / "app.py").write_text("value = 1\n", encoding="utf-8")
            nested = workspace.root / "nested"
            nested.mkdir()
            (nested / "bad.py").write_text("def broken(:\n", encoding="utf-8")
            nested_failed = await run_check({"workspace": str(workspace.root), "command": "compile"})
            self.assertNotEqual(nested_failed["exit_code"], 0)
            (nested / "bad.py").unlink()
            with self.assertRaises(ValueError):
                await run_check({"workspace": str(workspace.root), "command": "powershell"})
            skipped = await run_check({"workspace": str(workspace.root), "command": "unittest"})
            self.assertTrue(skipped["skipped"])
            tests = workspace.root / "tests"
            tests.mkdir()
            (tests / "test_wait.py").write_text(
                "import time, unittest\nclass Wait(unittest.TestCase):\n"
                " def test_wait(self): time.sleep(2)\n", encoding="utf-8")
            timed = await run_check({"workspace": str(workspace.root), "command": "unittest", "timeout": 0.1})
            self.assertFalse(timed["timed_out"])
            self.assertIsNone(timed["exit_code"])
            self.assertTrue(timed["skipped"])
            (tests / "test_wait.py").write_text(
                "import unittest\nclass Smoke(unittest.TestCase):\n"
                " def test_value(self): self.assertTrue(True)\n", encoding="utf-8")
            ran = await run_check({"workspace": str(workspace.root), "command": "unittest"})
            self.assertIsNone(ran["exit_code"])
            self.assertTrue(ran["skipped"])
            self.assertIn("stronger isolation", ran["summary"])
            self.assertIn("unittest discover", ran["command"])
        finally:
            workspace.close()

    async def test_bounded_correction_and_actual_test_result(self):
        calls = 0

        async def coder(payload):
            nonlocal calls
            calls += 1
            content = "API_KEY = 'private-value'\n" if calls == 1 else "value = 2\n"
            return {"changes": {"app.py": content}, "summary": "Changed app.py"}

        workflow = CodingWorkflow(self.root, planner, coder)
        report = await workflow.run("Change the value", ("app.py",), max_corrections=1)
        self.assertTrue(report.success)
        self.assertEqual(report.corrections, 1)
        self.assertEqual(report.changed_files, ("app.py",))
        self.assertEqual(report.tests[0]["exit_code"], 0)
        self.assertEqual((self.root / "app.py").read_text(), "value = 1\n")

        calls = 0
        limited = await workflow.run("Change the value", ("app.py",), max_corrections=0)
        self.assertFalse(limited.success)
        self.assertEqual(calls, 1)

    async def test_coder_cannot_claim_success_with_out_of_scope_or_invalid_result(self):
        async def escape(_):
            return {"changes": {"../outside.py": "x"}, "summary": "done"}

        report = await CodingWorkflow(self.root, planner, escape).run("Change value", ("app.py",))
        self.assertFalse(report.success)
        self.assertEqual((self.root / "app.py").read_text(), "value = 1\n")


if __name__ == "__main__":
    unittest.main()
