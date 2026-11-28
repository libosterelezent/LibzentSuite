"""Probe plugin: discovery/attachment/command-registry test fixture.

The framework itself ships with no bundled plugins; this fixture is the
plugin those suites exercise instead (discovery, per-account
instantiation, settings resolution, self-command declaration and
executor runs). It deliberately does nothing on events.

It is also the REFERENCE for the CONVERGED cog declaration: the
COG class carries typed converter params (the library resolves
mention/ID/name before the handler runs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands as _ext_commands

from libzentsuite.core.plugin_base import (
    PluginBase,
    PluginContext,
)
from libzentsuite.core.settings_manager import Setting

if TYPE_CHECKING:
    from typing import Any

    from libzentsuite.core.event_bus import Event


class ProbePlugin(PluginBase):
    """Minimal well-formed plugin (settings + declared commands)."""

    name = "probe"
    version = "1.1.0"
    description = "test fixture"

    settings = (
        Setting("trigger", "str", "go", "fixture trigger word"),
        Setting(
            "factor", "float", 2.0, "fixture multiplier (bounded)", min_value=0.1, max_value=10.0
        ),
    )

    def __init__(self) -> None:
        #: the live PluginContext of the current activation — command
        #: handlers reuse it (stashed on load like any stateful plugin)
        self._ctx: PluginContext | None = None

    # ------------------------------------------------------------ lifecycle
    async def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        ctx.logger.info("probe v%s loaded (trigger=%r)", self.version, ctx.settings["trigger"])

    async def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def on_disable(self, ctx: PluginContext) -> None:
        pass

    async def on_unload(self, ctx: PluginContext) -> None:
        self._ctx = None

    # ---------------------------------------------------------------- events
    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        return  # no-op: the fixture never reacts to traffic

    # ------------------------------------------------ command face (COG)
    #: the CONVERGED declaration: the cog host builds one per
    #: (account, plugin) with the toolbox injected; typed params
    #: are the grammar now (the legacy COMMANDS retired 2026-11-15)
    #: legacy COMMANDS — the cog host builds this per (account, plugin)
    #: with the toolbox injected; typed params replace ArgSpec grammar
    class _ProbeCog(_ext_commands.Cog):
        """The probe command face in converged form (typed params)."""

        def __init__(self, *, services: Any) -> None:
            self.services = services

        @_ext_commands.command(name="probeset_cog")
        async def probeset_cog(
            self, ctx: Any, name: str, mode: str, count: int, note: str = ""
        ) -> None:
            await ctx.send(f"set {name}:{mode}:{count}:{note or '-'} on {self.services.account}")

        @_ext_commands.command(name="probestart_cog")
        async def probestart_cog(self, ctx: Any, names: str) -> None:
            await ctx.send(f"start {names} on {self.services.account}")

    COG = _ProbeCog


__all__ = ["ProbePlugin"]
