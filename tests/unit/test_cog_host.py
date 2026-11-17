"""PluginCogHost tests (C1) — the host + toolbox contracts over the
REAL library (the fork is a pinned dependency; the standard cog-
testing pattern — direct invocation with constructed fixtures).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace as NS
from typing import Any

from discord.ext import commands

from core.plugin_manager.cog_host import CogServices, PluginCogHost


class SampleCog(commands.Cog):
    """A plugin-shaped cog: receives the toolbox, uses it."""

    def __init__(self, *, services: CogServices) -> None:
        self.services = services

    @commands.command(name="poke")
    async def poke(self, ctx: Any) -> None:
        await ctx.send(f"{self.services.account}:{self.services.plugin}")

    @commands.Cog.listener("on_message")
    async def on_message(self, message: Any) -> None:
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

    def test_paced_defaults_none_until_bound(self) -> None:
        # roadmap #4: fail-closed — an unbound toolbox exposes no
        # facade, never a fallback to some other account's pipeline
        services = CogServices(account="alpha", plugin="cleaner")
        self.assertIsNone(services.paced)

    def test_hosted_plugins_reports_per_account(self) -> None:
        # the reconciliation input: per-account hosted set, no leaks
        # across accounts
        host = PluginCogHost()

        self.assertEqual(host.hosted_plugins("alpha"), set())
        cog = SampleCog(services=host.services_for("alpha", "cleaner"))
        host._cogs[("alpha", "cleaner")] = cog
        host._cogs[("beta", "cleaner")] = cog
        self.assertEqual(host.hosted_plugins("alpha"), {"cleaner"})
        self.assertEqual(host.hosted_plugins("beta"), {"cleaner"})
        self.assertEqual(host.hosted_plugins("gamma"), set())

    def test_bind_paced_sets_and_replaces_the_facade(self) -> None:
        services = CogServices(account="alpha", plugin="cleaner")
        first, second = NS(kind="facade-1"), NS(kind="facade-2")
        services.bind_paced(first)
        self.assertIs(services.paced, first)
        services.bind_paced(second)  # re-attach rebinds
        self.assertIs(services.paced, second)

    def test_rebind_paced_noops_for_never_hosted_cogs(self) -> None:
        # rebind must never MINT a toolbox: only attach() creates, so
        # _attach_plugins' rebind loop cannot fabricate cogs
        host = PluginCogHost()
        host.rebind_paced("alpha", "cleaner", NS(kind="fresh"))
        self.assertNotIn(("alpha", "cleaner"), host._services)

    def test_rebind_paced_refreshes_a_hosted_toolbox(self) -> None:
        host = PluginCogHost()
        stale, fresh = NS(kind="stale-facade"), NS(kind="fresh-facade")
        toolbox = host.services_for("alpha", "cleaner")
        toolbox.bind_paced(stale)
        host.rebind_paced("alpha", "cleaner", fresh)
        self.assertIs(toolbox.paced, fresh)


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
        host = PluginCogHost(to_rich=_rich_converter)
        bot = _make_bot()
        await host.attach(bot, "alpha", "cleaner", SampleCog)
        sent: list[str] = []

        async def send(text: str) -> None:
            sent.append(text)

        ctx = NS(
            bot=bot,
            prefix="!",
            invoked_with="poke",
            command=bot.get_command("poke"),
            message=NS(attachments=[]),  # the parser reads attachments
            view=NS(),  # and the StringView
            send=send,
        )
        poke_command = bot.get_command("poke")
        assert poke_command is not None  # attached three lines above
        command_seam: Any = poke_command  # ctx is a duck-typed stand-in
        await command_seam.invoke(ctx)
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
        assert cog is not None  # attached above
        cog_seam: Any = cog  # on_message is a listener, not a Cog method
        await cog_seam.on_message(NS(id=42))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].message.id, 42)

    async def test_attach_binds_paced_facade_onto_the_toolbox(self) -> None:
        """roadmap #4: the account manager hands the per-(account,
        plugin) facade to attach; the cog's toolbox carries it so the
        client hook can lift it onto ctx.services."""
        host = PluginCogHost(to_rich=_rich_converter)
        bot = _make_bot()
        paced = NS(kind="paced-facade")
        cog = await host.attach(bot, "alpha", "cleaner", SampleCog, paced=paced)
        self.assertIs(cog.services.paced, paced)
        self.assertIsNotNone(bot.get_cog("SampleCog"))

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


class DirectClientConnectionTests(unittest.IsolatedAsyncioTestCase):
    """The owner-locked contract change (2026-11-17): the toolbox
    carries the account's library client at attach — the Mudabot-grade
    direct reach, account-scoped by construction."""

    async def test_attach_binds_the_bot_into_the_toolbox(self) -> None:
        host = PluginCogHost()
        bot = _make_bot()
        cog = await host.attach(bot, "alpha", "cleaner", SampleCog)
        self.assertIs(cog.services.client, bot)

    async def test_each_cog_sees_only_its_own_accounts_client(self) -> None:
        host = PluginCogHost()
        bot_a, bot_b = _make_bot(), _make_bot()
        await host.attach(bot_a, "alpha", "cleaner", SampleCog)
        cog_b = await host.attach(bot_b, "beta", "cleaner", SampleCog)
        self.assertIs(cog_b.services.client, bot_b)
        self.assertIsNot(cog_b.services.client, bot_a)

    async def test_unattached_toolbox_has_no_client(self) -> None:
        toolbox = PluginCogHost().services_for("alpha", "cleaner")
        self.assertIsNone(toolbox.client)

    async def test_detach_pops_the_connection_with_the_toolbox(self) -> None:
        host = PluginCogHost()
        bot = _make_bot()
        await host.attach(bot, "alpha", "cleaner", SampleCog)
        self.assertTrue(await host.detach("alpha", "cleaner"))
        toolbox = host.services_for("alpha", "cleaner")  # fresh after pop
        self.assertIsNone(toolbox.client)
