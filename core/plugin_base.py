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
    ``plugins/soulcopy/plugin.py``).

CONTRACT NOTE (A3 decision, documented for A6): the pinned PluginServices
protocol gained ``open_dm(user_id) -> int`` — soulcopy needs the Mudae DM
channel id to filter collected pages. The account manager implements it
(the underlying DiscordClient protocol already exposes ``open_dm``).
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from .priority import Priority

if TYPE_CHECKING:  # runtime duck-typing avoids import cycles / hard deps
    from .credentials import Credentials
    from .event_bus import Event, MessageReceived
    from .settings_manager import Setting
    from adapters.base import DiscordMessage


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

    # --------------------------------------------------------------- helpers
    @classmethod
    def setting_defaults(cls) -> Dict[str, Any]:
        """Manifest defaults as a plain dict (display/preview helper)."""
        return {s.name: s.default for s in cls.settings}

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

    def __repr__(self) -> str:
        return f"<PluginContext {self.account}:{self.plugin} settings={sorted(self.settings)}>"


@runtime_checkable
class PluginServices(Protocol):
    """Discord I/O surface handed to plugins (implemented by A6 per account).

    Every method rate-limits and audits through the shared send pipeline:
    ``send_message``/``send_dm`` submit into the task scheduler (priority
    LOW by default) — pacing/queueing happens behind this facade, so plugins
    never sleep to self-throttle.

    NOTE: ``open_dm`` was added by A3 (see module docstring) — soulcopy uses
    it to learn the Mudae DM channel id before collecting pages.

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
    ) -> "DiscordMessage":
        """Send to a guild/channel id through the task pipeline."""

    async def send_dm(
        self, user_id: int, content: str, *, priority: Priority = Priority.LOW
    ) -> "DiscordMessage":
        """Send a DM to a user id through the task pipeline."""

    async def open_dm(self, user_id: int) -> int:
        """Resolve (opening if needed) the DM channel id for a user."""

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


__all__ = ["PluginBase", "PluginContext", "PluginServices"]
