"""Probe plugin: discovery/attachment/command-registry test fixture.

The framework itself ships with no bundled plugins; this fixture is the
plugin those suites exercise instead (discovery, per-account
instantiation, settings resolution, self-command declaration and
executor runs). It deliberately does nothing on events.
"""
from __future__ import annotations

from typing import List, Optional

from core.event_bus import Event
from core.plugin_base import (
    CommandContext,
    CommandSpec,
    PluginBase,
    PluginContext,
)
from core.settings_manager import Setting


class ProbePlugin(PluginBase):
    """Minimal well-formed plugin (settings + one declared command)."""

    name = "probe"
    version = "1.0.0"
    description = "test fixture"

    settings = (
        Setting("trigger", "str", "go", "fixture trigger word"),
        Setting("factor", "float", 2.0,
                "fixture multiplier (bounded)",
                min_value=0.1, max_value=10.0),
    )

    def __init__(self) -> None:
        #: the live PluginContext of the current activation — command
        #: handlers reuse it (stashed on load like any stateful plugin)
        self._ctx: Optional[PluginContext] = None

    # ------------------------------------------------------------ lifecycle
    async def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        ctx.logger.info("probe v%s loaded (trigger=%r)",
                        self.version, ctx.settings["trigger"])

    async def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def on_disable(self, ctx: PluginContext) -> None:
        pass

    async def on_unload(self, ctx: PluginContext) -> None:
        self._ctx = None

    # ---------------------------------------------------------------- events
    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        return  # no-op: the fixture never reacts to traffic

    # -------------------------------------------------------- self-commands
    async def _cmd_probeinfo(self, ctx: CommandContext,
                             args: List[str]) -> str:
        """One-line status reply through the paced pipeline."""
        return "probe ok"

    #: declared LAST: the handler name above already exists in the class
    #: namespace (a class body is sequential)
    COMMANDS = (
        CommandSpec(
            name="probeinfo",
            help="show fixture info",
            usage="",
            category="Probe",
            handler=_cmd_probeinfo,
        ),
    )


__all__ = ["ProbePlugin"]
