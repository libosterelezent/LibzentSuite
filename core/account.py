"""Account model + session: one Discord user account managed by the container.

AccountSession wraps a DiscordClientProtocol (real adapter or test fake) and is
the ONLY path plugins use to reach Discord. Every outbound message/DM goes
through the central rate limiter before touching the client.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .events import EventBus, MessageEvent, ReadyEvent
from .rate_limiter import Priority, RateLimiterService

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class AccountError(RuntimeError):
    """Raised for invalid account configuration or invalid session state."""


class AccountStatus(str, Enum):
    CREATED = "created"
    CONNECTING = "connecting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class ReconnectPolicy:
    """Exponential-backoff reconnection rules for a dropped session.

    Delays grow initial_delay * multiplier**attempt, capped at max_delay.
    """

    enabled: bool = True
    initial_delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0

    def delay_for(self, attempt: int) -> float:
        """Backoff wait before Nth reconnect attempt (0-based)."""
        return min(self.initial_delay * (self.multiplier ** attempt), self.max_delay)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ReconnectPolicy":
        """Parse {'enabled','initial','max','multiplier'} with sane defaults."""
        data = data or {}
        return cls(
            enabled=bool(data.get("enabled", True)),
            initial_delay=float(data.get("initial", 1.0)),
            max_delay=float(data.get("max", 60.0)),
            multiplier=float(data.get("multiplier", 2.0)),
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "initial": self.initial_delay,
            "max": self.max_delay,
            "multiplier": self.multiplier,
        }


@dataclass
class AccountConfig:
    """Declarative per-account configuration (config/accounts/<name>.json)."""

    name: str
    token_ref: Optional[str] = None          # env key; default DISCORD_TOKEN_<NAME>
    enabled: bool = True
    plugins: Optional[List[str]] = None      # None => every discovered plugin
    settings: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # plugin->key->value
    rate_rules: Dict[str, str] = field(default_factory=dict)           # action->'n/secs'
    sync_group: Optional[str] = None         # accounts in the same group act in sync
    reconnect: Optional[Dict[str, Any]] = None  # ReconnectPolicy overrides

    def __post_init__(self) -> None:
        if not NAME_RE.match(self.name or ""):
            raise AccountError(
                f"account name {self.name!r} must match {NAME_RE.pattern} (lowercase)"
            )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AccountConfig":
        """Build from parsed JSON; unknown keys rejected to catch typos."""
        allowed = {"name", "token_ref", "enabled", "plugins", "settings", "rate_rules", "sync_group", "reconnect"}
        unknown = set(data) - allowed
        if unknown:
            raise AccountError(f"unknown account config keys: {sorted(unknown)}")
        return cls(
            name=data["name"],
            token_ref=data.get("token_ref"),
            enabled=bool(data.get("enabled", True)),
            plugins=data.get("plugins"),
            settings=data.get("settings") or {},
            rate_rules=data.get("rate_rules") or {},
            sync_group=data.get("sync_group"),
            reconnect=data.get("reconnect"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe serialization (never includes the token itself)."""
        return {
            "name": self.name,
            "token_ref": self.token_ref,
            "enabled": self.enabled,
            "plugins": self.plugins,
            "settings": self.settings,
            "rate_rules": self.rate_rules,
            "sync_group": self.sync_group,
            "reconnect": self.reconnect,
        }

    def effective_token_ref(self) -> str:
        """Env key holding this account's token."""
        return self.token_ref or f"DISCORD_TOKEN_{self.name.upper()}"


class DiscordMessage:
    """Client-agnostic message shape exchanged between client and session."""

    __slots__ = ("id", "channel_id", "author_id", "content", "dm")

    def __init__(self, id: int, channel_id: int, author_id: int, content: str, dm: bool = False):
        self.id = id
        self.channel_id = channel_id
        self.author_id = author_id
        self.content = content
        self.dm = dm


class AccountSession:
    """Runtime wrapper binding one account to a client + bus + limiter."""

    def __init__(
        self,
        config: AccountConfig,
        client: Any,
        bus: EventBus,
        limiter: RateLimiterService,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self._client = client
        self._bus = bus
        self._limiter = limiter
        self._logger = logger or logging.getLogger(f"mudae.account.{config.name}")
        self.status = AccountStatus.CREATED
        self.last_error: Optional[str] = None
        #: set when the connection drops UNEXPECTEDLY (watched by the supervisor)
        self.lost: "asyncio.Event[None]" = asyncio.Event()
        self._stopping = False
        # route every client message onto the bus as a MessageEvent
        self.attach(client)

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def user_id(self) -> Optional[int]:
        """Discord user id of the logged-in account, once connected."""
        return getattr(self._client, "user_id", None)

    def attach(self, client: Any) -> None:
        """Bind a client to this session (wires message + disconnect callbacks)."""
        self._client = client
        client.on_message = self._on_client_message
        client.on_disconnect = self._on_client_disconnect

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Connect the client; publishes ReadyEvent on success.

        Raises AccountError if already running; records failures as status=FAILED.
        """
        if self.status == AccountStatus.RUNNING:
            raise AccountError(f"account {self.name} is already running")
        self._stopping = False
        self.status = AccountStatus.CONNECTING
        try:
            await self._client.connect()
        except Exception as exc:
            self.status = AccountStatus.FAILED
            self.last_error = str(exc)
            raise AccountError(f"account {self.name} failed to connect: {exc}") from exc
        self.status = AccountStatus.RUNNING
        self.lost.clear()
        self._logger.info("account running (user_id=%s)", self.user_id)
        self._bus.publish_nowait(ReadyEvent(account=self.name))

    async def reattach(self, client: Any) -> None:
        """Swap in a FRESH client and reconnect (used by the supervisor)."""
        self.status = AccountStatus.CONNECTING
        self.attach(client)
        await client.connect()
        self.status = AccountStatus.RUNNING
        self.lost.clear()
        self._logger.info("account reconnected (user_id=%s)", self.user_id)
        self._bus.publish_nowait(ReadyEvent(account=self.name))

    async def stop(self) -> None:
        """Disconnect the client deliberately; safe to call multiple times."""
        if self.status == AccountStatus.STOPPED:
            return
        self._stopping = True  # suppress the lost-connection signal
        await self._client.disconnect()
        self.status = AccountStatus.STOPPED
        self._logger.info("account stopped")

    # ------------------------------------------------------------- messaging
    async def send_message(
        self, channel_id: int, content: str, priority: Priority | int = Priority.NORMAL
    ) -> DiscordMessage:
        """Rate-limited message send (action='message')."""
        await self._limiter.acquire("message", self.name, priority)
        return await self._client.send_message(channel_id, content)

    async def send_dm(
        self, user_id: int, content: str, priority: Priority | int = Priority.NORMAL
    ) -> DiscordMessage:
        """Rate-limited DM send (action='dm'); opens the DM channel if needed."""
        await self._limiter.acquire("dm", self.name, priority)
        channel_id = await self._client.open_dm(user_id)
        return await self._client.send_message(channel_id, content)

    async def fetch_user(self, user_id: int) -> Any:
        """Look up a user (returns None if unknown to the client)."""
        return await self._client.fetch_user(user_id)

    async def open_dm(self, user_id: int) -> int:
        """Return the DM channel id for a user, opening it if necessary."""
        return await self._client.open_dm(user_id)

    # -------------------------------------------------------------- internal
    def _on_client_message(self, message: DiscordMessage) -> None:
        """Sync client callback -> fire-and-forget bus publish."""
        self._bus.publish_nowait(
            MessageEvent(
                account=self.name,
                message_id=message.id,
                channel_id=message.channel_id,
                author_id=message.author_id,
                content=message.content,
                dm=message.dm,
            )
        )

    def _on_client_disconnect(self, reason: str = "") -> None:
        """Sync client callback on UNEXPECTED connection loss.

        Deliberate stop() sets _stopping first and never lands here. Marks the
        session RECONNECTING and sets `lost` for the container supervisor.
        """
        if self._stopping:
            return
        if self.status == AccountStatus.RUNNING:
            self.status = AccountStatus.RECONNECTING
            self.last_error = reason or "connection lost"
            self.lost.set()
            self._logger.warning("connection lost (%s)", self.last_error)
