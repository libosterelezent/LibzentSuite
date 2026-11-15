"""Ping plugin tests: discovery, self-command execution, UI action."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.errors import TargetGone
from core.plugin_base import PluginContext
from plugins.ping.plugin import PingPlugin


class _FakeServices:
    """The PluginServices surface ping touches (paced sends)."""

    def __init__(self, blocked: bool = False) -> None:
        self.blocked = blocked
        self.sent: list[tuple] = []

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

    return SimpleNamespace(account="t", plugin="ping",
                           settings={}, services=services,
                           logger=logging.getLogger("test.ping"))


class PingPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.plugin = PingPlugin()
        self.services = _FakeServices()
        self.ctx = _ctx(self.services)


    async def test_ping_sends_through_services(self) -> None:
        reply = await self.plugin._ping(self.ctx, 555, "hello there")
        self.assertEqual(self.services.sent, [(555, "hello there")])
        self.assertIn("sent to", reply)

