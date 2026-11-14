"""FrameworkVerbsCog — the framework's own verbs as a real Cog (C3).

The convergence's C3: framework verbs (status, pg, say, estop, help,
prefixrate, adminlist, adminperms, adminprefix, workspaces…) become a
library cog — ONE command model with plugin cogs. The handlers ride
the hub's api_provider (the same ControlAPI surface) with replies via
ctx.send (the documented command-reply seam).
"""

from __future__ import annotations

from typing import Any

from discord.ext import commands

__all__ = ["FrameworkVerbsCog"]


def _api(ctx: Any) -> Any:
    """The hub's api_provider — the ONE ControlAPI every verb hits."""
    from core.plugin_manager.commands import COMMAND_HUB

    return COMMAND_HUB.api_provider()


class FrameworkVerbsCog(commands.Cog):
    """The framework verbs, library-native."""

    def __init__(self, *, services: Any = None) -> None:
        self.services = services  # the toolbox (unused by verbs; symmetric)

    @commands.command(name="pg")
    async def pg(self, ctx, arg: str = "") -> None:
        api = _api(ctx)
        out = await api.status()
        await ctx.send(str(out.get("status", "unknown")))

    @commands.command(name="say")
    async def say(self, ctx, *, text: str) -> None:
        await ctx.send(text)

    @commands.command(name="status")
    async def status(self, ctx) -> None:
        api = _api(ctx)
        out = await api.status()
        await ctx.send(str(out))

    @commands.command(name="estop")
    async def estop(self, ctx) -> None:
        api = _api(ctx)
        await api.emergency_stop_all(surface="chat")
        await ctx.send("emergency stop engaged")

    @commands.command(name="adminlist")
    async def adminlist(self, ctx) -> None:
        api = _api(ctx)
        out = await api.admin_overview()
        await ctx.send(str(out))
