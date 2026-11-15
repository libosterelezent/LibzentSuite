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
    ``tests/fixtures/sim_plugins/exporter/plugin.py``, a test-only
    workload fixture).

CONTRACT NOTE (A3 decision, documented for A6): the pinned PluginServices
protocol gained ``open_dm(user_id) -> int`` — flow plugins need the target bot
DM channel id to filter collected pages. The account manager implements it
(the underlying DiscordClient protocol already exposes ``open_dm``).

SELF-COMMAND DECLARATION (per-account command interface): a plugin may
declare a ``COG`` class attribute — a library Cog subclass —
making its verbs available as ``<prefix><name>`` self-commands on every
account it is ASSIGNED to and ENABLED on (e.g. ``sm!rollstart``). Handlers
run through the SAME paced/isolated pipeline as plugin code: replies go
through ``ctx.services.send_message`` (never a raw client), a crashing
handler is contained by the executor, and the plugin manager binds each
declared handler to the per-account plugin instance before invocation
(the cog carries the command callbacks).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Protocol,
    runtime_checkable,
)

from .priority import Priority

if TYPE_CHECKING:
    from .workspace.models import FollowSpec

if TYPE_CHECKING:  # runtime duck-typing avoids import cycles / hard deps
    import logging
    from collections.abc import Callable, Sequence

    from .credentials import Credentials as Credentials
    from .event_bus import Event, MessageReceived
    from .settings_manager import Setting as Setting


class CommandSettingsApi(Protocol):
    """Settings surface a self-command handler may read/mutate."""

    def get(self, plugin: str, key: str, default: Any = None) -> Any: ...

    def set(self, plugin: str, key: str, value: Any) -> Any: ...

    def clear(self, plugin: str, key: str) -> Any: ...


class PluginBase(abc.ABC):  # noqa: B024 -- contract marker; every hook is optional by design
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
    settings: ClassVar[Sequence[Setting]] = ()
    #: self-commands the plugin declares — the CONVERGED form: a real
    #: library Cog class carrying the command face (typed converter
    #: params, command groups, native help). The loader validates it
    #: at discovery; the cog host builds one instance per
    #: (account, plugin) with the services toolbox injected.
    COG: ClassVar[Any] = None
    #: WORKSPACE chains (decision 2, DEFAULT OFF — chain-inert without
    #: them): bare signal names this plugin may emit at runtime via
    #: ``await ctx.services.emit_signal(name, payload)`` (namespacing
    #: ``<plugin>:<name>`` is added at consumption — plugins cannot
    #: spoof another plugin's signal), and the triggers this plugin
    #: follows, each naming one handler on the class. Validated at
    #: load time by ``core.workspace.validate_plugin_declarations``
    #: (a follow whose handler is missing is a load error; unknown
    #: signal namespaces warn).
    EMITS: ClassVar[tuple[str, ...]] = ()
    FOLLOWS: ClassVar[tuple[FollowSpec, ...]] = ()

    # ------------------------------------------------------ dashboard UI (C)

    # ------------------------------------------------------------- lifecycle
    async def on_load(self, ctx: PluginContext) -> None:  # noqa: B027  # optional lifecycle hook
        """Called once when the plugin is first enabled on an account."""

    async def on_enable(self, ctx: PluginContext) -> None:  # noqa: B027  # optional lifecycle hook
        """Called every time the plugin becomes active (after on_load)."""

    async def on_disable(self, ctx: PluginContext) -> None:  # noqa: B027  # optional lifecycle hook
        """Called when the plugin is disabled or the account stops."""

    async def on_unload(self, ctx: PluginContext) -> None:  # noqa: B027  # optional lifecycle hook
        """Called when the plugin is removed from the account (last call)."""

    # ---------------------------------------------------------------- events
    async def handle_event(self, ctx: PluginContext, event: Event) -> None:  # noqa: B027  # optional hook
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
    settings: dict[str, Any]
    #: per-(account, plugin) services facade (sends, waits, credentials)
    services: PluginServices
    #: pre-scoped logger: libzent.<account>.<plugin>
    logger: logging.Logger
    #: setting keys the user set EXPLICITLY in any settings layer (None =
    #: unknown / not computed). A resolved value equal to the schema
    #: default is ambiguous — it may be the default itself OR an explicit
    #: choice that happens to match it. Plugins that force a setting at
    #: runtime (e.g. an auto-disable latch) consult this to avoid
    #: clobbering an explicit user re-enable on the next load.
    explicit_settings: frozenset[str] | None = None

    def __repr__(self) -> str:
        return f"<PluginContext {self.account}:{self.plugin} settings={sorted(self.settings)}>"


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
    #: per-(account, plugin) services facade (paced sends, credentials...);
    #: None ONLY for framework verbs, which run without a plugin facade
    services: PluginServices | None
    #: pre-scoped logger: libzent.<account>.<plugin>
    logger: logging.Logger
    #: channel the command was invoked in
    channel_id: int = 0
    #: guild of the invoking channel (None for DMs)
    guild_id: int | None = None
    #: validated settings surface (None on stripped stacks)
    settings_api: CommandSettingsApi | None = None
    #: verbatim argument remainder (whitespace-normalized by the parser)
    raw: str = ""
    #: the invoking Discord user id (None when unknown) — admin-gated
    #: handlers check it against the AccessManager
    invoker_id: int | None = None
    #: Reserved: the converged cog path parses typed kwargs natively
    #: (the library's converters); this field is vestigial.
    parsed: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PluginServices(Protocol):
    """Discord I/O surface handed to plugins (implemented by A6 per account).

    Every method rate-limits and audits through the shared send pipeline:
    ``send_message``/``send_dm`` submit into the task scheduler (priority
    LOW by default) — pacing/queueing happens behind this facade, so plugins
    never sleep to self-throttle.

    NOTE: ``open_dm`` was added by A3 (see module docstring) — flow
    plugins use it to learn the primary bot's DM channel id before collecting
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

    async def add_reaction(
        self, channel_id: int, message_id: int, emoji: str, *, priority: Priority = Priority.NORMAL
    ) -> None:
        """React to a message through the paced pipeline (kakerareact)."""

    async def click_button(
        self,
        channel_id: int,
        message_id: int,
        custom_id: str,
        *,
        priority: Priority = Priority.NORMAL,
    ) -> None:
        """Click a message component by custom_id (kakeraloot) — paced and
        audited like a send."""

    async def fetch_user(self, user_id: int) -> Any:
        """Resolve a user (cached per account in the database); None when
        the account's client cannot resolve them."""

    async def cooldown_set(
        self,
        key: str,
        duration_s: float,
        *,
        source: str = "plugin",
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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

    async def cooldown_list(self) -> list[dict[str, Any]]:
        """Every ACTIVE cooldown of this account+plugin, soonest first."""

    async def state_get(self, key: str, default: Any = None) -> Any:
        """Read one persisted value from this account+plugin scope."""

    async def state_set(self, key: str, value: Any) -> None:
        """Persist one JSON-serializable value in this account+plugin scope."""

    async def state_delete(self, key: str) -> bool:
        """Delete one value from this account+plugin scope; True when it
        existed."""

    async def wait_for_message(
        self, check: Callable[[MessageReceived], bool], timeout: float
    ) -> MessageReceived | None:
        """Wait for one matching inbound message; None on timeout."""

    async def collect_messages(
        self, check: Callable[[MessageReceived], bool], idle_timeout: float
    ) -> list[MessageReceived]:
        """Collect matching inbound messages until ``idle_timeout`` passes
        with no new match (port of the audited container semantics)."""

    def publish(self, event: Event) -> None:
        """Publish an event on the shared bus (fire-and-forget safe)."""

    def validate_channel(self, channel_id: int) -> None:
        """Raise :class:`core.errors.TargetGone` when the channel is not
        usable for this account (call BEFORE sending — flow safety)."""

    def describe_channel(self, channel_id: int) -> str:
        """Human-readable channel label (``#name`` / DM partner)."""

    @property
    def credentials(self) -> Credentials:
        """Fleet credentials (primary bot id, owner ids) — never hardcoded."""

    @property
    def account(self) -> str:
        """Account name this facade belongs to."""

    @property
    def plugin(self) -> str:
        """Plugin name this facade was created for."""

    @property
    def settings(self) -> dict[str, Any]:
        """Resolved settings of this (account, plugin) pair."""


@dataclass(frozen=True)
class ArgSpec:
    """HUB-INTERNAL: one positional argument of a framework verb.

    No longer the plugin-facing grammar (plugins declare typed params
    on cog callbacks); this shape serves the framework verb
    definitions inside the hub until C3 migrates them to cogs."""

    name: str
    kind: Any = str
    required: bool = True
    default: Any = None
    greedy: bool = False
    choices: tuple[Any, ...] | None = None


def _convert_arg(spec: ArgSpec, token: str) -> Any:
    """HUB-INTERNAL: coerce one token to the ArgSpec's kind."""
    if spec.kind is str or (callable(spec.kind) is False and spec.kind is None):
        return token
    if spec.kind in (int, float):
        try:
            return spec.kind(token)
        except (TypeError, ValueError):
            raise ValueError(
                f"<{spec.name}> must be a valid {spec.kind.__name__}, got {token!r}"
            ) from None
    if callable(spec.kind):
        try:
            return spec.kind(token)
        except ValueError:
            raise ValueError(f"<{spec.name}>: {token!r} is not valid") from None
    raise ValueError(
        f"<{spec.name}> has an unsupported kind {spec.kind!r} (use str, int, float or a callable)"
    )


@dataclass(frozen=True)
class CommandSpec:
    """HUB-INTERNAL: one framework verb definition.

    The plugin-facing COMMANDS contract is RETIRED (plugins declare
    COG classes); this shape defines the hub's own verbs until C3
    migrates them to the FrameworkVerbsCog."""

    name: str
    help: str
    usage: str = ""
    category: str = ""
    args: tuple[ArgSpec, ...] = ()
    handler: Callable[..., Any] | None = None


def parse_command_args(spec: CommandSpec, argv: list[str]) -> dict[str, Any]:
    """HUB-INTERNAL: parse one framework verb invocation's positional
    arguments against its ArgSpec grammar. Raises ValueError with a
    usage-ready message on arity/type/choice failures."""
    out: dict[str, Any] = {}
    rest = list(argv)
    for arg in spec.args:
        if arg.greedy:
            out[arg.name] = list(rest)
            rest = []
            break
        if not rest:
            if arg.required:
                raise ValueError(f"missing <{arg.name}>")
            out[arg.name] = arg.default
            continue
        token = rest.pop(0)
        if arg.choices is not None and token not in arg.choices:
            raise ValueError(f"<{arg.name}> must be one of {list(arg.choices)}, got {token!r}")
        out[arg.name] = _convert_arg(arg, token)
    if rest:
        raise ValueError(f"unexpected extra argument(s): {' '.join(rest)}")
    return out


def usage_line(spec: CommandSpec) -> str:
    """HUB-INTERNAL: render a framework verb's usage hint."""
    parts = [f"<{a.name}>" if a.required else f"[{a.name}]" for a in spec.args]
    if any(a.greedy for a in spec.args):
        parts.append("...")
    return " ".join(parts)


__all__ = [
    "ArgSpec",
    "CommandContext",
    "CommandSettingsApi",
    "CommandSpec",
    "PluginBase",
    "PluginContext",
    "PluginServices",
    "parse_command_args",
    "usage_line",
]
