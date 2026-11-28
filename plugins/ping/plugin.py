"""Ping plugin: sends a provided message to a chosen channel.

The first REAL production plugin (the framework ships none by design —
this is the operator's first drop-in) and the first plugin COG (roadmap
#4): the cog host binds the per-(account, plugin) paced facade to the
toolbox at attach time, and the client's before_invoke lifts it onto
``ctx.services`` — so the command face rides the SAME paced pipeline
as all plugin traffic:

  * ``<prefix>ping <channel_id> <text...>`` — self-command that sends
    ``text`` to ``channel_id`` through ``ctx.services.send_message``
    (never a raw client) and replies via ``ctx.send`` — command
    responses are the ONE documented use of the library-native send,
  * ``FlowBlocked`` safety: a gone/forbidden channel aborts ONE clean
    reply line — the plugin contract (core/plugin_base.py docstring).
Named ``ping`` (not ``say``) because the framework cog reserves
``say`` — a plugin re-declaring it is a collision, not an override.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from discord.ext import commands

from libzentsuite.core.errors import FlowBlocked
from libzentsuite.core.plugin_base import PluginBase

if TYPE_CHECKING:
    from libzentsuite.core.event_bus import Event
    from libzentsuite.core.plugin_base import PluginContext, Setting
    from libzentsuite.core.plugin_manager.cog_host import CogServices

LOG = logging.getLogger("libzent.ping")


class PingCog(commands.Cog):
    """Ping's command face — the first plugin COG on the host."""

    def __init__(self, *, services: CogServices | None = None) -> None:
        # the CogServices toolbox the host injects; the paced facade
        # arrives on ctx (the client's before_invoke lifts it there)
        self.services = services

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context[Any], channel_id: int, *, text: str) -> None:
        """Send ``text`` to ``channel_id`` through the paced pipeline."""
        services = getattr(ctx, "services", None)
        if services is None:
            # fail-closed: no bound facade on this stack — say so,
            # never fall back to a raw client send
            await ctx.send("ping: the paced pipeline is not wired on this stack")
            return
        try:
            services.validate_channel(channel_id)
        except FlowBlocked as exc:
            LOG.info("ping: channel %s unusable — %s", channel_id, exc)
            await ctx.send(f"channel unusable: {exc}")
            return
        try:
            await services.send_message(channel_id, text)
        except FlowBlocked as exc:
            LOG.info("ping: send to %s blocked — %s", channel_id, exc)
            await ctx.send(f"send blocked: {exc}")
            return
        where = services.describe_channel(channel_id)
        LOG.info("ping: delivered to %s (%s)", where, channel_id)
        await ctx.send(f"sent to {where}")


class PingPlugin(PluginBase):
    """Send a provided message to a chosen channel (paced, audited)."""

    name = "ping"
    version = "2.0.0"
    description = "sends a provided message to a chosen channel"

    #: no settings — the channel + text arrive per invocation
    settings: ClassVar[tuple[Setting, ...]] = ()

    #: the command face — the cog host builds one PingCog per
    #: (account, plugin) with the toolbox injected
    COG = PingCog

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None

    # ------------------------------------------------------------ lifecycle
    async def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def on_disable(self, ctx: PluginContext) -> None:
        pass

    async def on_unload(self, ctx: PluginContext) -> None:
        self._ctx = None

    # ---------------------------------------------------------------- events
    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        return  # ping is command-driven; it never reacts to traffic


__all__ = ["PingCog", "PingPlugin"]
