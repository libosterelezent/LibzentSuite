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
