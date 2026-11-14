"""Probe plugin: discovery/attachment/command-registry test fixture.

The framework itself ships with no bundled plugins; this fixture is the
plugin those suites exercise instead (discovery, per-account
instantiation, settings resolution, self-command declaration and
executor runs). It deliberately does nothing on events.

It is also the REFERENCE for the declarative argument grammar
(:class:`ArgSpec`): ``probeset`` shows the original-MudaeScript-style
positional form (``rollset <profile_name> <roulette> <channel_id>
<userstack> <ignore_keys>``-shaped), ``probestart`` shows the greedy
multi-name form (``rollstart <p1> <p2> <p3>``-shaped).
"""
from __future__ import annotations

import time
from typing import Any

from discord.ext import commands as _ext_commands

from core.event_bus import Event
from core.plugin_base import (
    ArgSpec,
    CommandContext,
    CommandSpec,
    PluginBase,
    PluginContext,
)
from core.settings_manager import Setting


class ProbePlugin(PluginBase):
    """Minimal well-formed plugin (settings + declared commands)."""

    name = "probe"
    version = "1.1.0"
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
        self._ctx: PluginContext | None = None

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

    # ------------------------------------------------------- UI extension
    async def _probe_action(self, context, payload):
        return ActionResult(success=True, message="probe executed",
                            data={"ts": time.time()})

    async def _set_verbose(self, context, payload):
        return ActionResult(success=True,
                            message=f"verbose → {payload.get('value')}")

    # -------------------------------------------------------- self-commands
    async def _cmd_probeinfo(self, ctx: CommandContext,
                             args: list[str]) -> str:
        """One-line status reply through the paced pipeline."""
        return "probe ok"

    async def _cmd_probeset(self, ctx: CommandContext,
                            args: list[str]) -> str:
        """Positional-grammar demo: everything arrives typed+validated
        in ``ctx.parsed`` — the handler never parses strings itself."""
        p: dict[str, Any] = ctx.parsed
        return (f"probe set: name={p.get('name')} mode={p.get('mode')} "
                f"count={p.get('count')} note={p.get('note')!r}")

    async def _cmd_probestart(self, ctx: CommandContext,
                              args: list[str]) -> str:
        """Greedy-args demo: consumes every remaining token as a list
        (multi-target commands) with per-item verdicts, original-style."""
        names: list[str] = ctx.parsed.get("names") or []
        started = [n for n in names if not n.startswith("x")]
        skipped = [n for n in names if n.startswith("x")]
        parts = [f"started: {', '.join(started)}" if started else "",
                 f"skipped: {', '.join(skipped)}" if skipped else ""]
        return "\n".join(p for p in parts if p) or "nothing started"

    #: declared LAST: the handler names above already exist in the class
    #: namespace (a class body is sequential)
    #: C2 (migration window): the CONVERGED declaration alongside the
    #: legacy COMMANDS — the cog host builds this per (account, plugin)
    #: with the toolbox injected; typed params replace ArgSpec grammar
    class _ProbeCog(_ext_commands.Cog):
        """The probe command face in converged form (typed params)."""

        def __init__(self, *, services):
            self.services = services

        @_ext_commands.command(name="probeset_cog")
        async def probeset_cog(self, ctx, name: str, mode: str, count: int,
                               note: str = "") -> None:
            await ctx.send(
                f"set {name}:{mode}:{count}:{note or '-'}"
                f" on {self.services.account}"
            )

        @_ext_commands.command(name="probestart_cog")
        async def probestart_cog(self, ctx, names: str) -> None:
            await ctx.send(f"start {names} on {self.services.account}")

    COG = _ProbeCog

    COMMANDS = (
        CommandSpec(
            name="probeinfo",
            help="show fixture info",
            usage="",
            category="Probe",
            handler=_cmd_probeinfo,
        ),
        CommandSpec(
            name="probeset",
            help="set a named probe profile up",
            usage="",
            category="Probe",
            handler=_cmd_probeset,
            args=(
                ArgSpec("name"),
                ArgSpec("mode", choices=("fast", "slow", "auto")),
                ArgSpec("count", kind=int),
                ArgSpec("note", required=False, default=""),
            ),
        ),
        CommandSpec(
            name="probestart",
            help="start one or more probes by name",
            usage="",
            category="Probe",
            handler=_cmd_probestart,
            args=(ArgSpec("names", greedy=True),),
        ),
    )


__all__ = ["ProbePlugin"]
