"""Shared command routing used by terminal REPL and TUI."""
from __future__ import annotations

import unittest

from core.api import ControlAPI

from .helpers import Harness

from cli.commands import QuitRequested, format_limits, format_plugins, format_status_lines, run_command


def make_api(harness: Harness) -> ControlAPI:
    return ControlAPI(harness.container, harness.limiter, harness.loader, harness.credentials)


class TestRunCommand(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = Harness("tests/fixtures/plugins", ["alpha", "beta"])
        self.api = make_api(self.harness)

    async def test_help_lists_every_command(self) -> None:
        text = await run_command(self.api, "help")
        for command in ("status", "plugins", "set-limit", "env", "quit"):
            self.assertIn(command, text)

    async def test_status_renders_accounts(self) -> None:
        text = await run_command(self.api, "status")
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        self.assertIn("created", text)

    async def test_unknown_command(self) -> None:
        text = await run_command(self.api, "frobnicate")
        self.assertIn("unknown command", text)

    async def test_quit_raises(self) -> None:
        with self.assertRaises(QuitRequested):
            await run_command(self.api, "quit")
        with self.assertRaises(QuitRequested):
            await run_command(self.api, "exit")

    async def test_blank_line_is_silent(self) -> None:
        self.assertIsNone(await run_command(self.api, "   "))

    async def test_set_limit_and_limits_roundtrip(self) -> None:
        result = await run_command(self.api, "set-limit message 3/10s alpha")
        self.assertIn("3/10.0s", result)
        text = await run_command(self.api, "limits")
        self.assertIn("alpha", text)

    async def test_bad_usage_reports_usage(self) -> None:
        text = await run_command(self.api, "set-limit onlyonearg")
        self.assertIn("usage:", text)


class TestFormatters(unittest.TestCase):
    def test_format_status_lines(self) -> None:
        lines = format_status_lines(
            {
                "limits": {"pending": {}},
                "accounts": [
                    {
                        "name": "alpha",
                        "status": "running",
                        "sync_group": "grp",
                        "plugins": {"spy": {"enabled": True}},
                    }
                ],
            }
        )
        self.assertEqual(len(lines), 2)
        self.assertIn("alpha", lines[1])
        self.assertIn("running", lines[1])
        self.assertIn("spy+", lines[1])

    def test_format_limits_empty(self) -> None:
        self.assertEqual(format_limits({"actions": {}, "pending": {}}), ["  (no rules configured)"])

    def test_format_plugins_empty(self) -> None:
        self.assertEqual(format_plugins([]), ["  (no plugins discovered)"])


if __name__ == "__main__":
    unittest.main()
