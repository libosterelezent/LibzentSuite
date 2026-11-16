"""plugin_base — the plugin DRIVER contract (post-C2).

Plugins subclass PluginBase: identity ClassVars (name/version/
description/settings), the COG command face, EMITS/FOLLOWS chain
declarations, and the optional lifecycle hooks. At runtime every hook
receives a PluginContext whose .services is the paced PluginServices
facade. The hub-internal command grammar retired with the registry
(2026-11-15) — commands ARE cogs now; see dump/README.md.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Protocol,
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

    # ------------------------------------------------ plugin profiles (P3)
    # The profile lifecycle: bound to THIS (account, plugin) by the
    # facade — no scope parameters exist, so no cross-scope access can
    # be expressed. settings bodies are opaque to the core; defaults
    # and validation are the plugin's own code.
    async def profile_create(
        self,
        name: str,
        description: str = "",
        settings: dict[str, Any] | None = None,
        *,
        schema_version: int = 1,
    ) -> dict[str, Any]: ...

    async def profile_get(self, name: str) -> dict[str, Any] | None: ...

    async def profile_update(
        self,
        name: str,
        *,
        description: str | None = None,
        settings: dict[str, Any] | None = None,
        schema_version: int | None = None,
    ) -> dict[str, Any]: ...

    async def profile_delete(self, name: str) -> bool: ...

    async def profile_list(self) -> list[dict[str, Any]]: ...

    async def profile_set_active(self, name: str) -> None: ...

    async def profile_active(self) -> str | None: ...

    async def profile_settings(self) -> dict[str, Any] | None:
        """P4's resolution seam: the ACTIVE profile's body (None when
        unset) — resolve BEFORE the work; a profile switch changes what
        the NEXT resolution returns."""

    def publish(self, event: Event) -> None:
        """Publish an event on the shared bus (fire-and-forget safe)."""

    async def emit_signal(self, name: str, payload: dict[str, Any] | None = None) -> str:
        """Emit a workspace chain signal (W4, decision 12) — the durable
        outbox path; returns the signal id. Declared on the protocol so
        the seam the facade implements (audit finding #10, 2026-11-15)
        is visible to every plugin author and to the checker."""

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


__all__ = ["PluginBase", "PluginContext", "PluginServices"]
