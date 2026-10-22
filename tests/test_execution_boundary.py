"""Offline subprocess boundary tests with fake processes."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from specialists import ScopedWorkspace, TEST_COMMANDS, run_check


class FakeStream:
    def __init__(self, data=b""):
        self.data = data

    async def read(self, _size):
        data, self.data = self.data, b""
        return data


class FakeProcess:
    def __init__(self, *, blocked=False, output=b""):
        self.stdout = FakeStream(output)
        self.stderr = FakeStream(b"")
        self.returncode = None if blocked else 0
        self._finished = asyncio.Event()
        if not blocked:
            self._finished.set()
        self.killed = False

    async def wait(self):
        await self._finished.wait()
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._finished.set()


class ExecutionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.workspace = ScopedWorkspace(self.root, ("app.py",))

    def tearDown(self):
        self.workspace.close()
        self.temp.cleanup()

    async def test_allowlist_and_workspace_restriction(self):
        with self.assertRaises(ValueError):
            await run_check({"workspace": str(self.workspace.root), "command": "cmd /c dir"})
        with self.assertRaises(ValueError):
            await run_check({"workspace": str(self.workspace.root), "command": ["compile"]})
        with self.assertRaises(ValueError):
            await run_check({"workspace": str(self.root), "command": "compile"})

    async def test_environment_and_output_are_bounded(self):
        fake = FakeProcess(output=b"x" * 20_000)
        with patch.dict(os.environ, {"DISCORD_TOKEN": "must-not-inherit",
                                     "GITHUB_TOKEN": "must-not-inherit",
                                     "MAX_CHECK_OUTPUT_BYTES": "512"}):
            with patch("specialists.asyncio.create_subprocess_exec",
                       new_callable=AsyncMock, return_value=fake) as create:
                result = await run_check({"workspace": str(self.workspace.root), "command": "compile"})
        args, kwargs = create.call_args
        self.assertEqual(args, TEST_COMMANDS["compile"])
        self.assertEqual(kwargs["cwd"], str(self.workspace.root))
        self.assertNotIn("DISCORD_TOKEN", kwargs["env"])
        self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
        self.assertNotIn("PATH", kwargs["env"])
        self.assertLessEqual(len(result["output"]), 512)
        self.assertEqual(result["exit_code"], 0)

    async def test_timeout_requests_termination(self):
        fake = FakeProcess(blocked=True)
        with patch("specialists.asyncio.create_subprocess_exec",
                   new_callable=AsyncMock, return_value=fake):
            result = await run_check({"workspace": str(self.workspace.root),
                                      "command": "compile", "timeout": 0.1})
        self.assertTrue(fake.killed)
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])
        self.assertIn("termination", result["summary"])

    async def test_cancellation_requests_termination(self):
        fake = FakeProcess(blocked=True)
        started = asyncio.Event()

        async def create(*_args, **_kwargs):
            started.set()
            return fake

        with patch("specialists.asyncio.create_subprocess_exec", side_effect=create):
            runner = asyncio.create_task(run_check(
                {"workspace": str(self.workspace.root), "command": "compile"}))
            await started.wait()
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner
        self.assertTrue(fake.killed)

    async def test_symlink_is_rejected_if_supported(self):
        target = self.root / "outside.py"
        target.write_text("value = 2\n", encoding="utf-8")
        link = self.workspace.root / "link.py"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable on this Windows configuration")
        with self.assertRaises(ValueError):
            await run_check({"workspace": str(self.workspace.root), "command": "compile"})


if __name__ == "__main__":
    unittest.main()
