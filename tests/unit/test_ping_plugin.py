"""Ping plugin tests: the COG command face (roadmap #4) + discovery.

The command exercises the documented seams: ``ctx.services`` for the
paced send (lifted there by the client's before_invoke from the
toolbox the host binds) and ``ctx.send`` for the command reply.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from discord.ext import commands

from libzentsuite.core.errors import TargetGone
from plugins.ping.plugin import PingCog, PingPlugin


class _FakePacedFacade:
    """The PluginServices surface ping's command touches."""

    def __init__(self, blocked: bool = False, block_send: bool = False) -> None:
        self.blocked = blocked
        self.block_send = block_send
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, channel_id: int, content: str, **kw: Any) -> None:
        if self.block_send:
            raise TargetGone("t", f"channel {channel_id} is gone")
        self.sent.append((channel_id, content))

    def validate_channel(self, channel_id: int) -> None:
        if self.blocked:
            raise TargetGone("t", f"channel {channel_id} is gone")

    def describe_channel(self, channel_id: int) -> str:
        return f"#{channel_id}"


def _cog() -> PingCog:
    return PingCog(services=None)


def _ctx(services: _FakePacedFacade | None) -> Any:
    """A context double shaped like the fork's Context after the
    before_invoke lift: ``services`` set only when the facade is
    bound (fail-closed mirror of the real seam)."""
    replies: list[str] = []

    async def send(text: str) -> None:
        replies.append(text)

    ctx = SimpleNamespace(send=send, replies=replies)
    if services is not None:
        ctx.services = services
    return ctx


async def _invoke(cog: PingCog, ctx: Any, channel_id: int, text: str) -> None:
    """Drive the command callback directly — the library's full
    Command dispatch is exercised in test_cog_host; here the face."""
    # the library's declared callback type does not model cog-method
    # binding (the cog instance rides argument 1 at runtime)
    await cog.ping.callback(cog, ctx, channel_id, text=text)  # type: ignore[arg-type,misc]


class PingCogTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_sends_through_the_paced_facade(self) -> None:
        facade = _FakePacedFacade()
        ctx = _ctx(facade)
        await _invoke(_cog(), ctx, 555, text="hello there")
        self.assertEqual(facade.sent, [(555, "hello there")])
        self.assertEqual(ctx.replies, ["sent to #555"])

    async def test_command_replies_channel_unusable(self) -> None:
        facade = _FakePacedFacade(blocked=True)
        ctx = _ctx(facade)
        await _invoke(_cog(), ctx, 555, text="hello")
        self.assertEqual(facade.sent, [])
        self.assertEqual(len(ctx.replies), 1)
        self.assertIn("channel unusable", ctx.replies[0])

    async def test_command_replies_send_blocked(self) -> None:
        facade = _FakePacedFacade(block_send=True)
        ctx = _ctx(facade)
        await _invoke(_cog(), ctx, 555, text="hello")
        self.assertEqual(ctx.replies[0].startswith("send blocked"), True)

    async def test_command_unwired_stack_replies_guidance(self) -> None:
        # fail-closed: no facade on ctx -> the guidance reply is the
        # ONLY observable output (every send the command could make is
        # either the paced facade — absent — or ctx.send — recorded)
        ctx = _ctx(None)
        await _invoke(_cog(), ctx, 555, text="hello")
        self.assertEqual(len(ctx.replies), 1)
        self.assertIn("not wired", ctx.replies[0])


class PingPluginDeclarationTests(unittest.TestCase):
    def test_plugin_declares_the_cog(self) -> None:
        self.assertIs(PingPlugin.COG, PingCog)
        self.assertTrue(issubclass(PingPlugin.COG, commands.Cog))
        self.assertEqual(PingPlugin.name, "ping")

    def test_cog_command_name_is_ping(self) -> None:
        cog = _cog()
        self.assertIn("ping", {c.name for c in cog.get_commands()})
