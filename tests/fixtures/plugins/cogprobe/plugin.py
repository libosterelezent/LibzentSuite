"""A fixture plugin in CONVERGED form (C2 step 1: fixtures migrate
first). Declares its command face as a real library Cog — the shape
every plugin adopts when the COMMANDS surface flips.
"""

from __future__ import annotations

from discord.ext import commands

from core.plugin_base import PluginBase


class ProbeCog(commands.Cog):
    """The probe fixture's command face — typed params, native help."""

    def __init__(self, *, services) -> None:
        self.services = services  # the CogServices toolbox

    @commands.command(name="cogprobe")
    async def cogprobe(self, ctx, count: int = 1) -> None:
        await ctx.send(f"cog ok {self.services.account}:{count}")


class CogProbePlugin(PluginBase):
    name = "cogprobe"
    version = "1.0.0"
    description = "C2 fixture: a plugin whose commands are a real cog"

    #: the CONVERGED declaration: the cog class carrying the command
    #: face (typed annotations; the host builds one instance per
    #: (account, plugin) with the toolbox injected)
    COG = ProbeCog
