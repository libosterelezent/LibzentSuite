"""Account model + session: one Discord user account managed by the container.

AccountSession wraps a DiscordClientProtocol (real adapter or test fake) and is
the ONLY path plugins use to reach Discord. Every outbound message/DM goes
through the central rate limiter before touching the client.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
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
        """Parse {'enabled','initial','max','multiplier'} with sane defaults.

        AUDIT FIX: unknown keys are rejected (typo trap) — matching
        AccountConfig.from_dict's strictness.
        """
        data = data or {}
        allowed = {"enabled", "initial", "max", "multiplier", "initial_delay", "max_delay"}
        unknown = set(data) - allowed
        if unknown:
            raise AccountError(f"unknown reconnect policy keys: {sorted(unknown)}")
        return cls(
            enabled=bool(data.get("enabled", True)),
            initial_delay=float(data.get("initial", data.get("initial_delay", 1.0))),
            max_delay=float(data.get("max", data.get("max_delay", 60.0))),
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
    roles: List[str] = field(default_factory=list)  # capability grants (see core/roles.py)
    settings: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # plugin->key->value
    rate_rules: Dict[str, str] = field(default_factory=dict)           # action->'n/secs'
    sync_group: Optional[str] = None         # accounts in the same group act in sync
    reconnect: Optional[Dict[str, Any]] = None  # ReconnectPolicy overrides
    send_engine: Optional[Dict[str, Any]] = None  # SendPolicy overrides (FarmLite lane)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not NAME_RE.match(self.name or ""):
            raise AccountError(
                f"account name {self.name!r} must be a string matching {NAME_RE.pattern} (lowercase)"
            )
        if not isinstance(self.roles, list) or not all(isinstance(r, str) for r in self.roles):
            raise AccountError(f"account {self.name!r} roles must be a list of strings")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AccountConfig":
        """Build from parsed JSON; unknown keys rejected to catch typos."""
        if not isinstance(data, dict):
            raise AccountError(f"account config must be an object, got {type(data).__name__}")
        if "name" not in data:
            raise AccountError("account config needs a 'name' key")
        raw_roles = data.get("roles")
        if raw_roles is not None and not isinstance(raw_roles, list):
            raise AccountError(f"'roles' must be a list, got {type(raw_roles).__name__}")
        allowed = {"name", "token_ref", "enabled", "plugins", "roles", "settings", "rate_rules", "sync_group", "reconnect", "send_engine"}
        unknown = set(data) - allowed
        if unknown:
            raise AccountError(f"unknown account config keys: {sorted(unknown)}")
        return cls(
            name=data["name"],
            token_ref=data.get("token_ref"),
            enabled=bool(data.get("enabled", True)),
            plugins=data.get("plugins"),
            roles=list(data.get("roles") or []),
            settings=data.get("settings") or {},
            rate_rules=data.get("rate_rules") or {},
            sync_group=data.get("sync_group"),
            reconnect=data.get("reconnect"),
            send_engine=data.get("send_engine"),
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
            "roles": self.roles,
            "send_engine": self.send_engine,
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

    def __repr__(self) -> str:  # ERROR AUDIT: sane broadcast-result rendering
        preview = (self.content or "").replace("\n", "\\n")[:40]
        return f"<msg ch={self.channel_id} author={self.author_id}: {preview!r}>"


class AccountSession:
    """Runtime wrapper binding one account to a client + bus + limiter."""

    def __init__(
        self,
        config: AccountConfig,
        client: Any,
        bus: EventBus,
        limiter: RateLimiterService,
        logger: Optional[logging.Logger] = None,
        audit: Optional[Any] = None,
        engine: Optional[Any] = None,
    ) -> None:
        self.config = config
        self._client = client
        self._bus = bus
        self._limiter = limiter
        self._audit = audit
        self._engine = engine
        self._logger = logger or logging.getLogger(f"slaughtermudae.account.{config.name}")
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

        Raises AccountError if already running/connecting; records failures as
        status=FAILED. AUDIT FIX: a stop() issued DURING connect is honored —
        the session ends STOPPED instead of flipping back to RUNNING.
        """
        if self.status in (AccountStatus.RUNNING, AccountStatus.CONNECTING, AccountStatus.RECONNECTING):
            raise AccountError(f"account {self.name} is already {self.status.value}")
        self._stopping = False
        self.status = AccountStatus.CONNECTING
        try:
            await self._client.connect()
        except Exception as exc:
            self.status = AccountStatus.FAILED
            self.last_error = str(exc)
            raise AccountError(f"account {self.name} failed to connect: {exc}") from exc
        if self._stopping:  # stop() raced us mid-connect — honor it
            self.status = AccountStatus.STOPPED
            self._logger.info("account start aborted by concurrent stop")
            return
        self.status = AccountStatus.RUNNING
        self.lost.clear()
        self._logger.info("account running (user_id=%s)", self.user_id)
        self._bus.publish_nowait(ReadyEvent(account=self.name))

    async def reattach(self, client: Any) -> None:
        """Swap in a FRESH client and reconnect (used by the supervisor)."""
        self.status = AccountStatus.CONNECTING
        self.attach(client)
        await client.connect()
        connected = getattr(client, "connected", None)
        if self._stopping or connected is False:
            raise AccountError(f"account {self.name} reattach failed: client not connected")
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
        self.lost.clear()  # AUDIT FIX: defense-in-depth for direct-stop callers
        self._logger.info("account stopped")

    # ------------------------------------------------------------- messaging
    async def send_message(
        self, channel_id: int, content: str, priority: Priority | int = Priority.NORMAL
    ) -> DiscordMessage:
        """Rate-limited message send (action='message'), audited when enabled.

        SEND ENGINE: when the account has an engine attached, the actual
        send runs inside the serialized lane — two requests from this
        account can never be in flight simultaneously (FarmLite concept).
        """
        if self._engine is not None:
            timing: Dict[str, float] = {}
            submitted = time.perf_counter()

            def _factory():
                # lane just picked us up -> this IS the queue wait
                timing["engine_wait"] = time.perf_counter() - submitted
                return self._do_send_message(channel_id, content, priority, timing=timing)

            return await self._engine.run(None, _factory, priority=priority)
        return await self._do_send_message(channel_id, content, priority)

    async def _do_send_message(
        self,
        channel_id: int,
        content: str,
        priority: Priority | int = Priority.NORMAL,
        timing: Optional[Dict[str, float]] = None,
    ) -> DiscordMessage:
        started = time.perf_counter()
        grant = await self._limiter.acquire("message", self.name, priority)
        sent = await self._client.send_message(channel_id, content)
        self._audit_out("message", channel_id, content, grant, started, timing=timing)
        return sent

    async def send_dm(
        self, user_id: int, content: str, priority: Priority | int = Priority.NORMAL
    ) -> DiscordMessage:
        """Rate-limited DM send (action='dm'); serialized like messages."""
        if self._engine is not None:
            timing: Dict[str, float] = {}
            submitted = time.perf_counter()

            def _factory():
                timing["engine_wait"] = time.perf_counter() - submitted
                return self._do_send_dm(user_id, content, priority, timing=timing)

            return await self._engine.run(None, _factory, priority=priority)
        return await self._do_send_dm(user_id, content, priority)

    async def _do_send_dm(
        self,
        user_id: int,
        content: str,
        priority: Priority | int = Priority.NORMAL,
        timing: Optional[Dict[str, float]] = None,
    ) -> DiscordMessage:
        started = time.perf_counter()
        grant = await self._limiter.acquire("dm", self.name, priority)
        channel_id = await self._client.open_dm(user_id)
        sent = await self._client.send_message(channel_id, content)
        self._audit_out(
            "dm", channel_id, content, grant, started,
            note=f"to user {user_id}", timing=timing,
        )
        return sent

    async def fetch_user(self, user_id: int) -> Any:
        """Look up a user (returns None if unknown to the client)."""
        return await self._client.fetch_user(user_id)

    async def open_dm(self, user_id: int) -> int:
        """Return the DM channel id for a user, opening it if necessary."""
        return await self._client.open_dm(user_id)

    def cached_snapshot(self) -> Dict[str, Any]:
        """Client's in-memory view (user, guilds, channels, DMs) — never fetches.

        Delegates to the client's own cache (discord.py-self keeps guilds,
        channels and private channels in memory after READY). Used by the
        account details page; NO extra API calls are made.
        """
        snapshot = getattr(self._client, "cached_snapshot", None)
        return snapshot() if callable(snapshot) else {"available": False}

    # -------------------------------------------------------------- internal
    def _audit_out(
        self,
        action: str,
        channel_id: int,
        content: str,
        grant: Any,
        started: float,
        note: str = "",
        timing: Optional[Dict[str, float]] = None,
    ) -> None:
        """Record an outbound interaction with its limiter wait + applied delay.

        The send-engine queue wait (when a lane is attached) is captured at
        record time — appending after the send raced with inbound entries.
        """
        if self._audit is None or grant is None:
            return
        delay_ms = getattr(grant, "delay_applied", 0.0) * 1000.0
        wait_ms = max(0.0, (time.perf_counter() - started) * 1000.0 - delay_ms)
        if timing is not None and "engine_wait" in timing:
            note = f"{note} engine_wait={round(timing['engine_wait'] * 1000.0)}ms".strip()
        try:
            self._audit.record(
                account=self.name, direction="out", action=action,
                channel_id=channel_id, content=content,
                wait_ms=wait_ms, delay_ms=delay_ms, note=note,
            )
        except Exception:
            self._logger.exception("audit recording failed")

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
        AUDIT FIX: drops during CONNECTING (reattach handshake dying) also set
        `lost` so the supervisor can retry instead of hanging forever.
        """
        if self._stopping:
            return
        if self.status in (AccountStatus.RUNNING, AccountStatus.CONNECTING):
            self.status = AccountStatus.RECONNECTING
            self.last_error = reason or "connection lost"
            self.lost.set()
            self._logger.warning("connection lost (%s)", self.last_error)
