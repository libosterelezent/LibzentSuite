"""PluginCogHost tests (C1) — the host + toolbox contracts over the
REAL library (the fork is a pinned dependency; the standard cog-
testing pattern — direct invocation with constructed fixtures).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace as NS

from discord.ext import commands

from core.plugin_manager.cog_host import CogServices, PluginCogHost


class SampleCog(commands.Cog):
    """A plugin-shaped cog: receives the toolbox, uses it."""

    def __init__(self, *, services: CogServices) -> None:
        self.services = services

    @commands.command(name="poke")
    async def poke(self, ctx) -> None:
        await ctx.send(f"{self.services.account}:{self.services.plugin}")

    @commands.Cog.listener("on_message")
    async def on_message(self, message) -> None:
        # first-hand handling with the toolbox: rich parse + audit
        self.services.audit_rich(message)


def _rich_converter(message: Any) -> Any:
    return NS(id=message.id, parsed_by="framework-converter")


def _make_bot() -> commands.Bot:
    return commands.Bot(command_prefix="!", user_bot=True, owner_ids=set(), help_command=None)


class CogServicesTests(unittest.TestCase):
    def test_to_rich_returns_framework_conversion(self) -> None:
        services = CogServices(account="alpha", plugin="cleaner", to_rich=_rich_converter)
        rich = services.to_rich(NS(id=5))
        self.assertEqual(rich.parsed_by, "framework-converter")

    def test_to_rich_unwired_raises_honestly(self) -> None:
        services = CogServices(account="alpha", plugin="cleaner")
        with self.assertRaises(RuntimeError) as ctx:
            services.to_rich(NS(id=5))
        self.assertIn("not wired", str(ctx.exception))

    def test_audit_rich_publishes_the_inbound_event(self) -> None:
        published: list[NS] = []
        services = CogServices(
            account="alpha",
            plugin="cleaner",
            to_rich=_rich_converter,
            audit_publisher=lambda ev: published.append(ev),
            inbound_factory=lambda **kw: NS(kind="MessageReceived", **kw),
        )
        services.audit_rich(NS(id=7))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].kind, "MessageReceived")
        self.assertEqual(published[0].account, "alpha")
        self.assertEqual(published[0].message.parsed_by, "framework-converter")

    def test_audit_unwired_is_a_noop(self) -> None:
        services = CogServices(account="a", plugin="p")  # nothing wired
        services.audit_rich(NS(id=1))  # no raise


class PluginCogHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_registers_and_toolbox_flows(self) -> None:
        host = PluginCogHost(to_rich=_rich_converter)
        bot = _make_bot()
        cog = await host.attach(bot, "alpha", "cleaner", SampleCog)
        self.assertEqual(cog.services.account, "alpha")
        self.assertIn("SampleCog", bot.cogs)  # the library registered it
        self.assertIsNotNone(bot.get_cog("SampleCog"))

    async def test_command_invocation_reaches_the_cog(self) -> None:
        """The standard library drive: a typed ctx, the command runs,
        the reply carries the toolbox attribution."""
        import asyncio

        host = PluginCogHost(to_rich=_rich_converter)
        bot = _make_bot()
        await host.attach(bot, "alpha", "cleaner", SampleCog)
        sent: list[str] = []
        ctx = NS(
            bot=bot,
            prefix="!",
            invoked_with="poke",
            command=bot.get_command("poke"),
            message=NS(attachments=[]),  # the parser reads attachments
            view=NS(),  # and the StringView
            send=lambda text: sent.append(text) or asyncio.sleep(0),
        )
        await bot.get_command("poke").invoke(ctx)
        self.assertEqual(sent, ["alpha:cleaner"])

    async def test_listener_receives_the_toolbox_audit(self) -> None:
        published: list[NS] = []
        host = PluginCogHost(
            to_rich=_rich_converter,
            audit_publisher=lambda ev: published.append(ev),
            inbound_factory=lambda **kw: NS(kind="MessageReceived", **kw),
        )
        bot = _make_bot()
        await host.attach(bot, "alpha", "cleaner", SampleCog)
        # direct invocation — the deterministic listener pattern
        cog = bot.get_cog("SampleCog")
        await cog.on_message(NS(id=42))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].message.id, 42)

    async def test_detach_removes_the_cog(self) -> None:
        host = PluginCogHost(to_rich=_rich_converter)
        bot = _make_bot()
        await host.attach(bot, "alpha", "cleaner", SampleCog)
        self.assertIsNotNone(bot.get_cog("SampleCog"))
        self.assertTrue(await host.detach("alpha", "cleaner"))
        self.assertIsNone(bot.get_cog("SampleCog"))
        self.assertFalse(await host.detach("alpha", "cleaner"))  # idempotent


if __name__ == "__main__":
    unittest.main()
