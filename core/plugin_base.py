"""Plugin base class and runtime context.

A plugin is a subclass of PluginBase that:
  * declares metadata (name, version, description),
  * declares a settings manifest (list[SettingDef]) — everything it supports,
  * implements lifecycle hooks and handle_event() to react to Discord events.

The container instantiates ONE instance per (account, plugin) pair and hands it a
PluginContext carrying resolved settings, the account session (rate-limited
sending), credentials, limiter, logger and container helpers.
"""
from __future__ import annotations

import abc
import logging
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Optional, Sequence

from .settings import SettingDef

if TYPE_CHECKING:  # runtime duck-typing avoids import cycles
    from .account import AccountSession
    from .credentials import Credentials
    from .rate_limiter import Priority


class PluginBase(abc.ABC):
    """Contract every plugin module must fulfil."""

    #: unique plugin slug (lowercase, [a-z0-9_-])
    name: ClassVar[str] = "plugin"
    version: ClassVar[str] = "0.0.0"
    description: ClassVar[str] = ""
    #: everything the plugin supports, resolvable per account
    settings: ClassVar[Sequence[SettingDef]] = ()

    # ---------------------------------------------------------- lifecycle
    async def on_load(self, ctx: "PluginContext") -> None:
        """Called once when the plugin is first enabled on an account."""

    async def on_enable(self, ctx: "PluginContext") -> None:
        """Called every time the plugin becomes active."""

    async def on_disable(self, ctx: "PluginContext") -> None:
        """Called when the plugin is disabled or the account stops."""

    async def on_unload(self, ctx: "PluginContext") -> None:
        """Called when the plugin is removed from the account."""

    # -------------------------------------------------------------- events
    async def handle_event(self, ctx: "PluginContext", event: Any) -> None:
        """Dispatch point for every event seen by the owning account.

        Implementations MUST return promptly for unrelated events (filter early)
        because the container dispatches events sequentially per handler.
        """

    # ------------------------------------------------------------- helpers
    @classmethod
    def setting_defaults(cls) -> Dict[str, Any]:
        """Manifest defaults as a plain dict."""
        return {d.key: d.default for d in cls.settings}

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} v{self.version}>"


class PluginContext:
    """Everything a plugin may touch at runtime (all fields duck-typed)."""

    def __init__(
        self,
        account: str,
        plugin: str,
        settings: Dict[str, Any],
        session: "AccountSession",
        limiter: Any,
        credentials: "Credentials",
        container: Any,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        #: account name this instance runs as
        self.account = account
        #: plugin name
        self.plugin = plugin
        #: resolved+validated settings for THIS account
        self.settings = settings
        #: rate-limited Discord session facade (send_message/send_dm/...)
        self.session = session
        #: RateLimiterService (rarely needed directly; session already limits)
        self.limiter = limiter
        #: credentials — ids/tokens come from here, never hardcoded
        self.credentials = credentials
        #: container helpers: collect_messages/wait_for/broadcast
        self.container = container
        self.logger = logger or logging.getLogger(f"slaughtermudae.plugin.{account}.{plugin}")

    def __repr__(self) -> str:
        return f"<PluginContext {self.account}:{self.plugin} settings={sorted(self.settings)}>"
