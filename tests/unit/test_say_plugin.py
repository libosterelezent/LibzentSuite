"""Say plugin tests: discovery, self-command execution, UI action."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.errors import TargetGone  # noqa: E402
from core.plugin_base import CommandContext  # noqa: E402
from plugins.say.plugin import SayPlugin  # noqa: E402


class _FakeServices:
    """The PluginServices surface say touches (paced sends)."""

    def __init__(self, blocked: bool = False) -> None:
        self.blocked = blocked
        self.sent: List[tuple] = []

    async def send_message(self, channel_id: int, content: str,
                           **kw: Any) -> None:
        if self.blocked:
            raise TargetGone("t", f"channel {channel_id} is gone")
        self.sent.append((channel_id, content))

    def validate_channel(self, channel_id: int) -> None:
        if self.blocked:
            raise TargetGone("t", f"channel {channel_id} is gone")

    def describe_channel(self, channel_id: int) -> str:
        return f"#{channel_id}"


def _ctx(services: _FakeServices) -> SimpleNamespace:
    import logging

    return SimpleNamespace(account="t", plugin="say",
                           settings={}, services=services,
                           logger=logging.getLogger("test.say"))


def _cmd_ctx(services: _FakeServices) -> CommandContext:
    import logging

    return CommandContext(
        account="t", plugin="say", services=services,
        logger=logging.getLogger("test.say"), channel_id=1,
        guild_id=None, invoker_id=42, raw="", parsed={})


class SayPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.plugin = SayPlugin()
        self.services = _FakeServices()
        self.ctx = _ctx(self.services)

    async def test_command_declared_with_grammar(self) -> None:
        spec = SayPlugin.COMMANDS[0]
        self.assertEqual(spec.name, "say")
        self.assertEqual(
            [a.name for a in spec.args], ["channel_id", "text"])
        self.assertTrue(spec.args[1].greedy)

    async def test_say_sends_through_services(self) -> None:
        reply = await self.plugin._say(self.ctx, 555, "hello there")
        self.assertEqual(self.services.sent, [(555, "hello there")])
        self.assertIn("sent to", reply)
        self.assertEqual(len(self.plugin._sent), 1)

    async def test_blocked_channel_aborts_clean_no_raise(self) -> None:
        self.services.blocked = True
        reply = await self.plugin._say(self.ctx, 555, "hello")
        self.assertIn("unusable", reply)
        self.assertEqual(self.plugin._sent, [])

    async def test_cmd_requires_channel_and_text(self) -> None:
        cmd = _cmd_ctx(self.services)
        reply = await SayPlugin._cmd_say(self.plugin, cmd, [])
        self.assertIn("usage", reply)

    async def test_cmd_rejects_non_numeric_channel(self) -> None:
        cmd = _cmd_ctx(self.services)
        reply = await SayPlugin._cmd_say(self.plugin, cmd,
                                         ["abc", "hi"])
        self.assertIn("number", reply)

    async def test_cmd_happy_path(self) -> None:
        cmd = _cmd_ctx(self.services)
        reply = await SayPlugin._cmd_say(
            self.plugin, cmd, ["555", "hello", "world"])
        self.assertIn("sent to", reply)
        self.assertEqual(self.services.sent,
                         [(555, "hello world")])

    async def test_ui_action_requires_both_fields(self) -> None:
        self.plugin._ctx = self.ctx
        out = await self.plugin._ui_say(None, {"say-channel": 1})
        self.assertFalse(out["success"])
        self.assertIn("field_errors", out)

    async def test_ui_action_happy_path(self) -> None:
        self.plugin._ctx = self.ctx
        out = await self.plugin._ui_say(
            None, {"say-channel": 555, "say-text": "from dashboard"})
        self.assertTrue(out["success"])
        self.assertEqual(self.services.sent,
                         [(555, "from dashboard")])

    async def test_ui_stats_count(self) -> None:
        await self.plugin._say(self.ctx, 1, "x")
        cards = await self.plugin.ui_stats(self.ctx)
        self.assertEqual(cards[0].value, "1")


if __name__ == "__main__":
    unittest.main()
