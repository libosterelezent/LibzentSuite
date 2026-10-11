"""Plugin contract: base class, runtime context, services protocol (spec §4-6).

Ported from the audited ``core/plugin_base.py`` (git aa76da4) onto the rewrite
spine:

  * settings manifests now use :class:`core.settings_manager.Setting`,
  * Discord I/O goes ONLY through the :class:`PluginServices` protocol
    (implemented per account by the account manager, A6) — plugins never
    touch a client, a limiter or the bus directly,
  * the runtime context is a plain dataclass built by the plugin manager
    (one context per (account, plugin) pair).

Plugin contract (unchanged from the audited original):
  * declare metadata (``name`` — lowercase ``[a-z0-9_-]+`` — ``version``,
    ``description``) and a settings manifest (``settings``),
  * implement lifecycle hooks (``on_load``/``on_enable``/``on_disable``/
    ``on_unload``) and :meth:`PluginBase.handle_event`,
  * catch :class:`core.errors.FlowBlocked` around any multi-step flow, log
    ONE clean abort line and return — per-account isolation is the
    dispatcher's job, failing clean and quiet is the plugin's (reference:
    ``tests/fixtures/sim_plugins/soulcopy/plugin.py``, a test-only
    workload fixture).

CONTRACT NOTE (A3 decision, documented for A6): the pinned PluginServices
protocol gained ``open_dm(user_id) -> int`` — flow plugins need the Mudae
DM channel id to filter collected pages. The account manager implements it
(the underlying DiscordClient protocol already exposes ``open_dm``).

SELF-COMMAND DECLARATION (per-account command interface): a plugin may
declare a ``COMMANDS`` class attribute — a tuple of :class:`CommandSpec` —
making its verbs available as ``<prefix><name>`` self-commands on every
account it is ASSIGNED to and ENABLED on (e.g. ``sm!rollstart``). Handlers
run through the SAME paced/isolated pipeline as plugin code: replies go
through ``ctx.services.send_message`` (never a raw client), a crashing
handler is contained by the executor, and the plugin manager binds each
declared handler to the per-account plugin instance before invocation
(handlers may be plain methods — see CommandSpec).
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Dict,
    FrozenSet,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from .priority import Priority

if TYPE_CHECKING:  # runtime duck-typing avoids import cycles / hard deps
    from .credentials import Credentials
    from .event_bus import Event, MessageReceived
    from .settings_manager import Setting
    from adapters.base import DiscordMessage


@dataclass(frozen=True)
class StatCard:
    """One metric card on the Home tab (plugin-contributed).

    The dashboard renders these in its own metric-card template —
    plugins provide structured values, never HTML.
    """
    label: str
    value: str
    sub: str = ""
    level: str = ""          # "" | "ok" | "warn" | "err" (color)


@dataclass(frozen=True)
class PanelSection:
    """One section of a plugin sidebar panel (structured data only).

    kind: "metrics"  -> data = [{"label","value","sub","level"}]
          "table"    -> data = {"columns": [...], "rows": [[...], ...]}
          "list"     -> data = [{"label","value"}] (key-value list)
          "text"     -> data = str (rendered as a paragraph, escaped)
    """
    kind: str
    title: str = ""
    data: Any = None


@dataclass(frozen=True)
class UIPanel:
    """A plugin's sidebar panel — appears as a nav entry + content area.

    The dashboard renders each panel with its own templates; plugin
    values are esc()'d at render time (structured data, never raw HTML).
    """
    id: str                     # unique: "plugin_name:panel_id"
    title: str
    icon: str = ""              # SVG path data (24x24 viewBox); empty = default
    sections: Tuple[PanelSection, ...] = ()


class PluginBase(abc.ABC):
    """Contract every plugin module must fulfil.

    A plugin module discovered by :class:`core.plugin_manager.loader.PluginLoader`
    exports one subclass of this class. The plugin manager instantiates ONE
    instance per (account, plugin) pair and drives it through the lifecycle
    hooks, handing each hook/dispatch a :class:`PluginContext`.
    """

    #: unique plugin slug (strictly lowercase ``[a-z0-9_-]+``)
    name: ClassVar[str] = "plugin"
    #: human-facing version string (display only)
    version: ClassVar[str] = "0.0.0"
    #: one-line description for CLI/dashboard listings
    description: ClassVar[str] = ""
    #: everything the plugin supports; resolved per account by SettingsManager
    settings: ClassVar[Sequence["Setting"]] = ()
    #: self-commands the plugin declares (per-account command interface).
    #: Declared ONCE on the class — the loader validates the tuple at
    #: discovery and the plugin manager binds each handler to the
    #: per-(account, plugin) instance. See :class:`CommandSpec`.
    COMMANDS: ClassVar[Tuple["CommandSpec", ...]] = ()

    # ---------------------------------------------------------- UI extension
    async def ui_stats(self, ctx: "PluginContext") -> Sequence["StatCard"]:
        """Stat cards this plugin contributes to the Home tab.

        Called when the dashboard fetches /api/ui/stats. Return a list
        of StatCard(label, value, sub, level) — the dashboard renders
        them in its own metric template (structured data, never HTML).
        """
        return ()

    async def ui_panels(self, ctx: "PluginContext") -> Sequence["UIPanel"]:
        """Sidebar panels this plugin contributes.

        Called when the dashboard fetches /api/ui/panels. Each UIPanel
        becomes a nav entry + content area rendered from its structured
        sections (metrics/table/list/text) — never raw HTML.
        """
        return ()

    # ------------------------------------------------------------- lifecycle
    async def on_load(self, ctx: "PluginContext") -> None:
        """Called once when the plugin is first enabled on an account."""

    async def on_enable(self, ctx: "PluginContext") -> None:
        """Called every time the plugin becomes active (after on_load)."""

    async def on_disable(self, ctx: "PluginContext") -> None:
        """Called when the plugin is disabled or the account stops."""

    async def on_unload(self, ctx: "PluginContext") -> None:
        """Called when the plugin is removed from the account (last call)."""

    # ---------------------------------------------------------------- events
    async def handle_event(self, ctx: "PluginContext", event: "Event") -> None:
        """Dispatch point for every ``MessageReceived`` seen by the account.

        Implementations MUST filter fast for unrelated events (check the
        event type and the trigger word before anything async): the manager
        routes every inbound message to every enabled plugin, so a slow
        prologue here is multiplied by the plugin count.
        """

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} v{self.version}>"


@dataclass
class PluginContext:
    """Everything a plugin may touch at runtime.

    Built by the plugin manager; ``settings`` is the *live* resolved dict —
    ``PluginManager.set_setting`` updates it in place, so a long-lived
    context sees setting changes without being rebuilt.
    """

    #: account name this instance runs as
    account: str
    #: plugin name
    plugin: str
    #: resolved + validated settings for THIS account (live view)
    settings: Dict[str, Any]
    #: per-(account, plugin) services facade (sends, waits, credentials)
    services: "PluginServices"
    #: pre-scoped logger: slaughtermudae.<account>.<plugin>
    logger: logging.Logger
    #: setting keys the user set EXPLICITLY in any settings layer (None =
    #: unknown / not computed). A resolved value equal to the schema
    #: default is ambiguous — it may be the default itself OR an explicit
    #: choice that happens to match it. Plugins that force a setting at
    #: runtime (e.g. an auto-disable latch) consult this to avoid
    #: clobbering an explicit user re-enable on the next load.
    explicit_settings: Optional[FrozenSet[str]] = None

    def __repr__(self) -> str:
        return f"<PluginContext {self.account}:{self.plugin} settings={sorted(self.settings)}>"


@dataclass(frozen=True)
class ArgSpec:
    """One declarative positional argument of a self-command.

    This is the ENDPOINT for original-MudaeScript-style grammar:
    ``rollset <profile_name> <roulette> <channel_id> <userstack>
    <ignore_keys>`` is five ArgSpecs — the executor parses, converts and
    validates them BEFORE the handler runs, filling ``ctx.parsed``;
    failures render the auto-generated usage line back to the invoker
    (exactly like the original's MissingRequiredArgument handlers).
    """
    #: placeholder name — renders as ``<name>`` in usage lines
    name: str
    #: str | int | float, or any callable(text) -> value (raises
    #: ValueError on bad input)
    kind: Any = str
    required: bool = True
    default: Any = None
    #: consume the REMAINING tokens as a list (``rollstop <p...>``)
    greedy: bool = False
    #: allowed values (checked AFTER conversion)
    choices: Optional[Tuple[Any, ...]] = None


def _convert_arg(spec: "ArgSpec", token: str) -> Any:
    if spec.kind is str or callable(spec.kind) is False and spec.kind is None:
        value: Any = token
    elif spec.kind in (int, float):
        try:
            value = spec.kind(token)
        except (TypeError, ValueError):
            raise ValueError(
                f"<{spec.name}> must be a valid "
                f"{spec.kind.__name__}, got {token!r}") from None
    elif callable(spec.kind):
        try:
            value = spec.kind(token)
        except ValueError:
            raise ValueError(f"<{spec.name}>: {token!r} is not valid") from None
    else:
        value = token
    if spec.choices is not None and value not in spec.choices:
        allowed = "/".join(str(c) for c in spec.choices)
        raise ValueError(
            f"<{spec.name}> must be one of: {allowed} (got {token!r})")
    return value


def parse_command_args(spec: "CommandSpec", argv: List[str]) -> Dict[str, Any]:
    """Parse ``argv`` against the command's declared ArgSpecs.

    Returns the name->value dict for ``ctx.parsed``. Raises ValueError
    with a one-line reason on: missing required args, failed type
    conversion, disallowed choices, or leftover tokens.
    """
    out: Dict[str, Any] = {}
    rest = list(argv or [])
    for arg in spec.args:
        if arg.greedy:
            taken = list(rest)
            rest = []
            if not taken:
                if arg.required:
                    raise ValueError(f"missing <{arg.name}> (one or more)")
                taken = list(arg.default or [])
            out[arg.name] = taken
            continue
        if not rest:
            if arg.required:
                raise ValueError(f"missing <{arg.name}>")
            out[arg.name] = arg.default
            continue
        out[arg.name] = _convert_arg(arg, rest.pop(0))
    if rest:
        raise ValueError(
            f"unexpected argument {rest[0]!r} — this command takes "
            f"{len(spec.args)} argument(s)")
    return out


def usage_line(spec: "CommandSpec") -> str:
    """Auto-generated usage: ``<a> <b> [c...]`` from the ArgSpecs
    (empty when the command declares none — falls back to spec.usage)."""
    parts = []
    for arg in spec.args:
        if arg.greedy:
            parts.append(f"[{arg.name}...]" if not arg.required
                         else f"<{arg.name}...>")
        else:
            parts.append(f"<{arg.name}>" if arg.required
                         else f"[{arg.name}]")
    return " ".join(parts)


@dataclass(frozen=True)
class CommandSpec:
    """One plugin-declared self-command (Mudae-style grammar).

    Declared on the plugin class as part of ``COMMANDS`` (the ONE
    declaration mechanism — there is no runtime/on_load registration; the
    class attribute keeps the registry static, discoverable and sortable
    by manifest order).

    ``name`` is the BARE name without the account's command prefix
    (``rollstart``, not ``sm!rollstart``) and must match ``[a-z0-9_]+``.
    ``handler`` is ``async def handler(ctx: CommandContext, args) -> str``
    — the string it returns is sent back through the paced pipeline as a
    normal self-message in the invoking channel. Raising ``ValueError``
    (which includes :class:`core.errors.SettingsValidationError`) renders
    the usage line back to the invoker instead. The handler may be a
    PLAIN METHOD of the plugin class: the plugin manager binds it to the
    per-(account, plugin) instance, so handlers reach the plugin's own
    state and internals exactly like lifecycle hooks do.

    An empty ``category`` defaults to the plugin's display name at
    collection time; the auto-help groups commands by it.
    """

    #: bare command name WITHOUT prefix, e.g. "rollstart"
    name: str
    #: one-liner for the categorized help
    help: str
    #: argument usage, e.g. "<key> <value>" (display-only hint; the
    #: declarative ArgSpecs below are the enforced contract)
    usage: str = ""
    #: declarative positional arguments — when present the executor
    #: parses/validates them and fills ``ctx.parsed`` before the handler
    args: Tuple[ArgSpec, ...] = ()
    #: defaults to the plugin's display name
    category: str = ""
    #: async def handler(ctx: CommandContext, args: List[str]) -> str
    handler: Optional[Callable] = None


@runtime_checkable
class CommandSettingsApi(Protocol):
    """Validated settings surface for self-command handlers.

    Implemented by the plugin manager per (account, plugin) pair and
    handed to handlers as ``ctx.settings_api`` (None on stripped-down
    test stacks — handlers must degrade gracefully). This is the SAME
    validation path as the dashboard/CLI ``set-setting`` surface
    (:meth:`core.plugin_manager.PluginManager.set_setting`) and the SAME
    effective-settings merge as the Phase-1 account view (plugin Setting
    defaults <- bound profile (minus the reserved ``channels`` key) <-
    per-account overrides, with the profile's channel scope riding along
    under ``"channels"``).
    """

    def schema(self) -> Sequence["Setting"]:
        """The plugin's setting schema (declared order)."""

    async def effective(self) -> Dict[str, Any]:
        """Merged effective settings incl. the reserved ``channels`` scope."""

    async def set(self, key: str, value: Any) -> Any:
        """Validate + persist one per-account override; returns typed value.

        Raises SettingsValidationError for unknown keys / bad values.
        """

    async def clear(self, key: str) -> Any:
        """Remove one per-account override; returns the newly-effective value."""

    async def binding(self) -> Optional[str]:
        """Name of the account's bound profile for this plugin (None = none)."""

    async def profiles(self) -> List[Dict[str, Any]]:
        """The plugin's available profiles (``name``/``description`` rows)."""


@dataclass(frozen=True)
class CommandContext:
    """Everything a self-command handler may touch at runtime.

    Built per invocation by the plugin manager. ``services`` is the SAME
    per-(account, plugin) facade plugin code uses, so a handler's sends
    flow through the paced pipeline with identical guarantees — there is
    deliberately NO raw client access here. ``channel_id``/``guild_id``
    describe where the command was invoked (DM commands have
    ``guild_id=None``); ``raw`` is the verbatim argument remainder after
    the command word.
    """

    #: account name the command was invoked on
    account: str
    #: plugin name the command belongs to ("selfcmd" for framework verbs)
    plugin: str
    #: per-(account, plugin) services facade (paced sends, credentials...)
    services: "PluginServices"
    #: pre-scoped logger: slaughtermudae.<account>.<plugin>
    logger: logging.Logger
    #: channel the command was invoked in
    channel_id: int = 0
    #: guild of the invoking channel (None for DMs)
    guild_id: Optional[int] = None
    #: validated settings surface (None on stripped stacks)
    settings_api: Optional[CommandSettingsApi] = None
    #: verbatim argument remainder (whitespace-normalized by the parser)
    raw: str = ""
    #: the invoking Discord user id (None when unknown) — admin-gated
    #: handlers check it against the AccessManager
    invoker_id: Optional[int] = None
    #: PARSED arguments when the spec declares ArgSpecs (name -> typed
    #: value; greedy args are lists). Empty dict for spec-less commands
    #: (they read the raw ``args`` list instead).
    parsed: Dict[str, Any] = field(default_factory=dict)



@runtime_checkable
class PluginServices(Protocol):
    """Discord I/O surface handed to plugins (implemented by A6 per account).

    Every method rate-limits and audits through the shared send pipeline:
    ``send_message``/``send_dm`` submit into the task scheduler (priority
    LOW by default) — pacing/queueing happens behind this facade, so plugins
    never sleep to self-throttle.

    NOTE: ``open_dm`` was added by A3 (see module docstring) — flow
    plugins use it to learn the Mudae DM channel id before collecting
    pages.

    PERSISTENT PLUGIN API (public, documented): ``cooldown_*`` and ``state_*``
    give plugins durable, account-scoped persistence without touching SQL
    or connections (spec §20/§22):

      * ``cooldown_set/cooldown_remaining/cooldown_active/cooldown_clear/
        cooldown_list`` — persistent cooldowns for THIS (account, plugin)
        pair; absolute expiry survives restarts with the correct remaining
        time,
      * ``state_get/state_set/state_delete`` — a small JSON key/value store
        scoped to THIS (account, plugin) pair.

    Both families raise :class:`core.errors.PluginError` with a clear
    message when the framework did not wire the underlying service (e.g. a
    stripped-down test stack).
    """

    async def send_message(
        self, channel_id: int, content: str, *, priority: Priority = Priority.LOW
    ) -> None:
        """Send to a guild/channel id through the task pipeline.

        Fire-and-await-ack: returns ``None`` on success and RAISES on
        failure — the pipeline produces a human summary, not a round-tripped
        DiscordMessage, so callers must not use a return value (the
        round-3 annotation fix; the implementing facade documents the same
        contract from its side).
        """

    async def send_dm(
        self, user_id: int, content: str, *, priority: Priority = Priority.LOW
    ) -> None:
        """Send a DM to a user id through the task pipeline
        (fire-and-await-ack — see :meth:`send_message`)."""

    async def open_dm(self, user_id: int) -> int:
        """Resolve (opening if needed) the DM channel id for a user."""

    async def add_reaction(self, channel_id: int, message_id: int, emoji: str,
                           *, priority: Priority = Priority.NORMAL) -> None:
        """React to a message through the paced pipeline (kakerareact)."""

    async def click_button(self, channel_id: int, message_id: int,
                           custom_id: str, *,
                           priority: Priority = Priority.NORMAL) -> None:
        """Click a message component by custom_id (kakeraloot) — paced and
        audited like a send."""

    async def fetch_user(self, user_id: int) -> Any:
        """Resolve a user (cached per account in the database); None when
        the account's client cannot resolve them."""

    async def cooldown_set(self, key: str, duration_s: float, *,
                           source: str = "plugin", reason: str = "",
                           metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Start (or replace) a persistent cooldown for this account+plugin.

        ``duration_s`` counts from NOW; the absolute expiry is persisted so
        restarts keep the correct remaining time. Returns the cooldown row.
        """

    async def cooldown_remaining(self, key: str) -> float:
        """Seconds left on a cooldown (0.0 when none/expired)."""

    async def cooldown_active(self, key: str) -> bool:
        """True when a cooldown is currently active for this key."""

    async def cooldown_clear(self, key: str) -> bool:
        """Remove one cooldown; True when it existed."""

    async def cooldown_list(self) -> List[Dict[str, Any]]:
        """Every ACTIVE cooldown of this account+plugin, soonest first."""

    async def state_get(self, key: str, default: Any = None) -> Any:
        """Read one persisted value from this account+plugin scope."""

    async def state_set(self, key: str, value: Any) -> None:
        """Persist one JSON-serializable value in this account+plugin scope."""

    async def state_delete(self, key: str) -> bool:
        """Delete one value from this account+plugin scope; True when it
        existed."""

    async def wait_for_message(
        self, check: Callable[["MessageReceived"], bool], timeout: float
    ) -> Optional["MessageReceived"]:
        """Wait for one matching inbound message; None on timeout."""

    async def collect_messages(
        self, check: Callable[["MessageReceived"], bool], idle_timeout: float
    ) -> List["MessageReceived"]:
        """Collect matching inbound messages until ``idle_timeout`` passes
        with no new match (port of the audited container semantics)."""

    def publish(self, event: "Event") -> None:
        """Publish an event on the shared bus (fire-and-forget safe)."""

    def validate_channel(self, channel_id: int) -> None:
        """Raise :class:`core.errors.TargetGone` when the channel is not
        usable for this account (call BEFORE sending — flow safety)."""

    def describe_channel(self, channel_id: int) -> str:
        """Human-readable channel label (``#name`` / DM partner)."""

    @property
    def credentials(self) -> "Credentials":
        """Fleet credentials (mudae id, owner ids) — never hardcoded."""

    @property
    def account(self) -> str:
        """Account name this facade belongs to."""

    @property
    def plugin(self) -> str:
        """Plugin name this facade was created for."""

    @property
    def settings(self) -> Dict[str, Any]:
        """Resolved settings of this (account, plugin) pair."""


__all__ = ["PluginBase", "PluginContext", "PluginServices", "CommandSpec",
           "CommandContext", "CommandSettingsApi",
           "StatCard", "PanelSection", "UIPanel"]
