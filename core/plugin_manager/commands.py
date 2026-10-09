"""Self-command hub: the per-account command registry (Phase 3).

Connects three parties WITHOUT new wiring in the account manager or the
bootstrap (both outside this phase's ownership):

  * the :class:`~core.plugin_manager.PluginManager` (the registry SOURCE)
    publishes an account's command surface whenever assignments or
    enabled-flags change — the same paths that already re-attach plugins
    (``attach_account`` / ``set_enabled`` / ``detach_account``);
  * each account's live Discord client (the registry CONSUMER) subscribes
    on connect and rebuilds its ``ext.commands`` Bot registrations from
    the published spec list;
  * invocations flow back through :meth:`SelfCommandHub.invoke`, which
    delegates to the bound manager's ``execute_self_command`` — handlers
    run with plugin-grade pacing (replies go through the per-plugin
    services facade into the task pipeline, NEVER a raw client send).

FRAMEWORK COMMANDS: ``pg`` and ``say`` (category "Utilities") and the
dynamic ``help`` ("No Category") are declared HERE, not by plugins —
matching Mudae's layout. They are first-class :class:`CommandSpec`s, so
the registry treats them exactly like plugin-declared verbs (and the
``help`` listing is built dynamically from whatever the registry holds —
``snipewatch``-style extras a later plugin declares appear in the help
with zero framework changes).

COLLISION POLICY (deterministic): framework names are reserved, then
plugin specs are collected in manifest order (plugin name sort); the
FIRST registration of a name wins and every later collision is dropped
with a logged warning naming both plugins.

The hub is a process-wide singleton (like the adapter's IdentifyGate):
one framework runs per process. Tests that build several managers bind
the last one (documented last-wins) — executor-level tests can call
``PluginManager.execute_self_command`` directly, bypassing the hub.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..plugin_base import CommandContext, CommandSpec

LOG = logging.getLogger("slaughtermudae.selfcommands")

#: services-facade plugin label used for framework-owned commands (task
#: attribution: the audit rows read ``<account>/selfcmd``)
FRAMEWORK_PLUGIN = "selfcmd"

#: category headers, in the order Mudae uses: plugin categories (sorted,
#: from the specs) come first, then Utilities, then No Category.
UTILITIES_CATEGORY = "Utilities"
NO_CATEGORY = "No Category"

#: hard cap on one command reply (Discord's own limit is 2000; the same
#: headroom convention as core.remote_control)
MAX_REPLY_CHARS = 1900


def _fmt_uptime(seconds: Optional[float]) -> str:
    """Human uptime: 1h23m / 4m05s / 12s ('-' when unknown)."""
    if seconds is None or seconds < 0:
        return "-"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# ------------------------------------------------------- framework handlers
async def _pg_handler(ctx: CommandContext, args: List[str]) -> str:
    """Gateway latency + uptime of THIS account (self-command layer)."""
    stats = COMMAND_HUB.runtime_stats(ctx.account)
    latency = stats.get("latency_ms")
    uptime = stats.get("uptime_s")
    latency_text = f"{latency:.0f}ms" if isinstance(latency, (int, float)) else "-"
    return (f"{ctx.account}: gateway {latency_text} · "
            f"uptime {_fmt_uptime(uptime)}")


async def _say_handler(ctx: CommandContext, args: List[str]) -> str:
    """Repeat the invoker's message verbatim (whitespace-normalized)."""
    if not args:
        raise ValueError("say needs something to repeat")
    return " ".join(args)


async def _help_handler(ctx: CommandContext, args: List[str]) -> str:
    """Categorized listing of every registered command (dynamic)."""
    specs = COMMAND_HUB.specs(ctx.account)

    specs = COMMAND_HUB.specs(ctx.account)
    if args:  # help <command>: usage + description
        wanted = args[0].lower()
        for spec in specs:
            if spec.name == wanted:
                lines = [f"{spec.name} — {spec.help}"]
                if spec.usage:
                    lines.append(f"usage: {spec.name} {spec.usage}")
                else:
                    lines.append(f"usage: {spec.name}")
                lines.append(f"category: {spec.category or NO_CATEGORY}")
                return "\n".join(lines)
        known = " ".join(sorted(s.name for s in specs))
        raise ValueError(f"no command {wanted!r} (known: {known})")
    return render_help(specs)


#: framework-declared commands (reserved names; collision policy: a
#: plugin re-declaring one of these is dropped with a warning)
FRAMEWORK_COMMANDS: Sequence[CommandSpec] = (
    CommandSpec(
        name="pg",
        help="Gateway latency and uptime",
        usage="",
        category=UTILITIES_CATEGORY,
        handler=_pg_handler,
    ),
    CommandSpec(
        name="say",
        help="Repeats the user's message.",
        usage="<text>",
        category=UTILITIES_CATEGORY,
        handler=_say_handler,
    ),
    CommandSpec(
        name="help",
        help="Shows this message",
        usage="[command]",
        category="",  # renders under "No Category" like Mudae
        handler=_help_handler,
    ),
)

_FRAMEWORK_NAMES = frozenset(spec.name for spec in FRAMEWORK_COMMANDS)


def render_help(specs: Sequence[CommandSpec]) -> str:
    """The categorized ``help`` text for one account's spec list.

    Layout (Mudae-style)::

        Roll:
          rollinfo    ...
          rolllist    ...
        Utilities:
          pg          ...
          say         Repeats the user's message.
        No Category:
          help        Shows this message

    Plugin categories first (sorted, title-cased), then Utilities, then
    No Category; inside a category commands are name-sorted and the
    help column is aligned to the longest name.
    """
    groups: Dict[str, List[CommandSpec]] = {}
    for spec in specs:
        category = spec.category or NO_CATEGORY
        groups.setdefault(category, []).append(spec)
    plugin_categories = sorted(
        c for c in groups if c not in (UTILITIES_CATEGORY, NO_CATEGORY))
    ordered = plugin_categories + [
        c for c in (UTILITIES_CATEGORY, NO_CATEGORY) if c in groups]
    width = max((len(s.name) for s in specs), default=0) + 2
    width = max(width, 10)
    lines: List[str] = []
    for category in ordered:
        header = category if category == NO_CATEGORY else category.title()
        lines.append(f"{header}:")
        for spec in sorted(groups[category], key=lambda s: s.name):
            lines.append(f"  {spec.name.ljust(width)}{spec.help}")
    return "\n".join(lines) if lines else "no commands registered"


# --------------------------------------------------------------------- hub
@dataclass(frozen=True)
class RegisteredCommand:
    """One registry entry: the (bound) spec + the plugin it came from."""

    spec: CommandSpec
    #: plugin name ("roll"...) or FRAMEWORK_PLUGIN for pg/say/help
    plugin: str


class SelfCommandHub:
    """Process-wide account → command-surface registry.

    The plugin manager publishes; each account's live client subscribes;
    invocations route back to the manager. All state is guarded for the
    reconnect cycle (a new client replaces the old subscriber) and for
    test isolation (a new manager rebinds; stale subscribers raising are
    contained).
    """

    def __init__(self) -> None:
        self._manager: Any = None
        #: account -> registry entries (spec order = registration order)
        self._entries: Dict[str, List[RegisteredCommand]] = {}
        #: account -> the live client's rebuild callback (latest wins)
        self._subscribers: Dict[str, Callable[[List[CommandSpec]], None]] = {}
        #: account -> zero-arg stats provider (latency/uptime, set by the
        #: client when it subscribes)
        self._stats: Dict[str, Callable[[], Dict[str, Any]]] = {}

    # ---------------------------------------------------------- bindings
    def bind(self, manager: Any) -> None:
        """Bind the process's plugin manager (last-wins; tests rebind)."""
        self._manager = manager

    def unbind(self, manager: Any) -> None:
        """Drop the binding if THIS manager still owns it (shutdown)."""
        if self._manager is manager:
            self._manager = None

    # ----------------------------------------------------------- publish
    def refresh(self, account: str) -> None:
        """Recompute one account's registry and notify its subscriber.

        Called by the plugin manager from the SAME paths that re-attach
        plugins (assignment mutations, enable toggles) — so the client's
        Bot registrations always track the assigned ∧ enabled set.
        """
        entries = self._collect(account)
        self._entries[account] = entries
        subscriber = self._subscribers.get(account)
        if subscriber is None:
            return
        try:
            subscriber([entry.spec for entry in entries])
        except Exception:
            # a replaced/reconnecting client: contained, never fatal —
            # the NEXT subscriber (fresh connect) pulls fresh state.
            LOG.debug("self-command subscriber failed for %r", account,
                      exc_info=True)

    def forget(self, account: str) -> None:
        """Drop every trace of an account (detached/removed cascade)."""
        self._entries.pop(account, None)
        self._subscribers.pop(account, None)
        self._stats.pop(account, None)

    def _collect(self, account: str) -> List[RegisteredCommand]:
        """Framework commands first (reserved), then the manager's
        plugin entries — first registration of a name wins."""
        entries: List[RegisteredCommand] = []
        for spec in FRAMEWORK_COMMANDS:
            entries.append(RegisteredCommand(spec=spec, plugin=FRAMEWORK_PLUGIN))
        manager = self._manager
        if manager is None:
            return entries
        collector = getattr(manager, "command_entries", None)
        if collector is None:
            return entries
        try:
            plugin_entries = collector(account)
        except Exception:
            LOG.exception("self-command collection failed for %r", account)
            return entries
        for spec, plugin in plugin_entries:
            if spec.name in _FRAMEWORK_NAMES:
                LOG.warning(
                    "self-command %r declared by plugin %r collides with a "
                    "reserved framework name — the plugin's is dropped",
                    spec.name, plugin)
                continue
            entries.append(RegisteredCommand(spec=spec, plugin=plugin))
        return entries

    # --------------------------------------------------------- subscribe
    def subscribe(
        self,
        account: str,
        rebuild: Callable[[List[CommandSpec]], None],
        stats_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        """A live client subscribes: register the rebuild callback, then
        REFRESH from the bound manager and deliver the result — a fresh
        connect never serves entries a previous manager computed (test
        boots reuse account names; production restarts are new processes)."""
        self._subscribers[account] = rebuild
        if stats_provider is not None:
            self._stats[account] = stats_provider
        self.refresh(account)

    def unsubscribe(self, account: str) -> None:
        """The client disconnected/stopped: drop its callback and stats
        provider (a reconnect re-subscribes both)."""
        self._subscribers.pop(account, None)
        self._stats.pop(account, None)

    # ------------------------------------------------------------- reads
    def entries(self, account: str) -> List[RegisteredCommand]:
        """Current registry entries (fresh recompute — never stale)."""
        if account not in self._entries:
            self.refresh(account)
        return list(self._entries.get(account, ()))

    def specs(self, account: str) -> List[CommandSpec]:
        """Just the specs (help rendering / registration)."""
        return [entry.spec for entry in self.entries(account)]

    def runtime_stats(self, account: str) -> Dict[str, Any]:
        """Latest latency/uptime from the account's client ({} = none)."""
        provider = self._stats.get(account)
        if provider is None:
            return {}
        try:
            stats = provider() or {}
            return stats if isinstance(stats, dict) else {}
        except Exception:
            return {}

    # ---------------------------------------------------------- invoke
    async def invoke(
        self,
        account: str,
        name: str,
        args: List[str],
        channel_id: int,
        *,
        guild_id: Optional[int] = None,
        invoker_id: Optional[int] = None,
        raw: str = "",
    ) -> None:
        """Route one invocation to the bound manager (fully contained).

        The manager's executor builds the CommandContext, runs the
        handler with plugin-grade isolation and sends the reply through
        the paced pipeline. Unknown commands and an unbound manager are
        dropped quietly (the Bot layer never dispatches unknown names
        anyway — CommandNotFound).
        """
        manager = self._manager
        if manager is None:
            LOG.debug("self-command %r dropped: no manager bound", name)
            return
        execute = getattr(manager, "execute_self_command", None)
        if execute is None:
            return
        try:
            await execute(
                account, name, args, channel_id,
                guild_id=guild_id, invoker_id=invoker_id, raw=raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            # the executor is responsible for its own containment; this
            # guard exists so a BROKEN executor can never kill the Bot's
            # command dispatch task
            LOG.exception("self-command execution failed: %s!%s", account, name)


__all__ = [
    "SelfCommandHub", "RegisteredCommand", "FRAMEWORK_COMMANDS",
    "FRAMEWORK_PLUGIN", "UTILITIES_CATEGORY", "NO_CATEGORY",
    "MAX_REPLY_CHARS", "render_help", "COMMAND_HUB",
]


#: the process-wide hub (one framework per process; managers bind to it)
COMMAND_HUB = SelfCommandHub()
