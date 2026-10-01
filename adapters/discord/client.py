"""Real Discord client adapter (ported from the audited core/discord_adapter.py).

Bridges the :class:`adapters.base.DiscordClient` protocol to a live
user-account-capable Discord client library (e.g. ``discord.py-self``).

Ported, audit-proven behaviour:
  * the library import is LAZY (inside :meth:`RealDiscordClient.connect`) so
    tests, simulations and CLI dry-runs never require the library;
  * startup errors FAIL the connect instead of silently yielding a session
    with ``user_id=None`` (audit round 2 fix);
  * a client that reports ready without a user id is treated as a bad token;
  * ``cached_snapshot()`` builds EXACTLY from what the live client already
    holds in cache after READY — never a network fetch.

New in the rewrite:
  * library HTTP errors are translated into the shared domain vocabulary
    (:class:`core.errors.ServerRateLimit` / ``AccessDenied`` / ``TargetGone``)
    so the scheduler can react without knowing the library;
  * ``latency_ms`` and the ``Retry-After`` / ``X-RateLimit-Global`` header
    intake required by the send pipeline (INTEGRATION.md).

NOTE: selfbots violate Discord ToS; the framework keeps pacing conservative.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Callable, Optional

from adapters.base import DiscordMessage, convert_message
from core.credentials import Credentials
from core.errors import (
    AccessDenied,
    AccountError,
    CredentialsError,
    ServerRateLimit,
    TargetGone,
)

LOG = logging.getLogger("slaughtermudae.adapter")


def _header(headers: Any, name: str) -> Optional[str]:
    """Case-tolerant header lookup (library header dicts vary in casing)."""
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        return value
    except Exception:
        return None


class RealDiscordClient:
    """DiscordClient protocol implementation over a lazily imported library."""

    def __init__(self, token: str, logger: Optional[logging.Logger] = None) -> None:
        self._token = token
        self._discord: Any = None          # the discord module (set in connect)
        self._client: Any = None           # the live Client instance
        self._ready = asyncio.Event()
        self._startup_error: Optional[Exception] = None
        self._logger = logger or LOG
        #: best-effort account label for domain error messages (set by factory)
        self._account = ""
        #: callbacks assigned by the account manager BEFORE connect()
        self.on_message: Optional[Callable[[DiscordMessage], None]] = None
        self.on_disconnect: Optional[Callable[[str], None]] = None
        self.user_id: Optional[int] = None

    # ------------------------------------------------------------ labelling
    @property
    def account(self) -> str:
        """Best-effort account name (used to enrich AccessDenied reasons)."""
        return self._account

    @account.setter
    def account(self, name: str) -> None:
        self._account = str(name or "")

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        """Import the library, wire callbacks, start the client, await ready.

        Raises:
            AccountError: library missing, client failed to start, or the
                client connected without a user id (bad token).
        """
        try:
            import discord  # type: ignore[import-untyped]  # discord.py-self fork
        except ImportError as exc:
            raise AccountError(
                "live mode requires a user-account-capable discord client "
                "library (e.g. pip install discord.py-self)"
            ) from exc
        self._discord = discord
        # discord.py-self 2.x removed Intents (user accounts don't use them);
        # construct plain and fall back cleanly across fork variants
        try:
            self._client = discord.Client()
        except TypeError:
            self._client = discord.Client(intents=None)
        self._ready = asyncio.Event()
        self._startup_error = None

        @self._client.event
        async def on_ready() -> None:
            self.user_id = self._client.user.id
            self._ready.set()

        @self._client.event
        async def on_disconnect() -> None:
            if self.on_disconnect is not None:
                self.on_disconnect("gateway closed")

        @self._client.event
        async def on_message(message: Any) -> None:
            if self.on_message is None or message.author.id == self.user_id:
                return
            # RICH BOUNDARY: capture everything the event already holds
            # (embeds/components/attachments/reactions/reference/author/
            # channel/guild/created_at + the runtime-only raw handle) in ONE
            # non-blocking conversion — plugins never re-fetch it (§15)
            self.on_message(
                convert_message(
                    message,
                    dm=isinstance(message.channel, self._discord.DMChannel),
                    viewer_id=self.user_id,
                )
            )

        @self._client.event
        async def on_message_edit(before: Any, after: Any) -> None:
            if self.on_message_edit is not None and after.author.id != self.user_id:
                self.on_message_edit(convert_message(
                    after, dm=isinstance(after.channel, self._discord.DMChannel)))

        @self._client.event
        async def on_message_delete(message: Any) -> None:
            if self.on_message_delete is not None and message.author.id != self.user_id:
                guild = getattr(message, "guild", None)
                self.on_message_delete(
                    message.id, message.channel.id,
                    getattr(guild, "id", None) if guild else None, message,
                )

        @self._client.event
        async def on_reaction_add(reaction: Any, user: Any) -> None:
            if self.on_reaction is not None and user.id != self.user_id:
                message = reaction.message
                guild = getattr(message, "guild", None)                     or getattr(getattr(message, "channel", None), "guild", None)
                self.on_reaction(
                    "add", message.id, message.channel.id,
                    getattr(guild, "id", None) if guild else None,
                    user.id, reaction.emoji, (reaction, user),
                )

        @self._client.event
        async def on_reaction_remove(reaction: Any, user: Any) -> None:
            if self.on_reaction is not None and user.id != self.user_id:
                message = reaction.message
                guild = getattr(message, "guild", None)                     or getattr(getattr(message, "channel", None), "guild", None)
                self.on_reaction(
                    "remove", message.id, message.channel.id,
                    getattr(guild, "id", None) if guild else None,
                    user.id, reaction.emoji, (reaction, user),
                )

        asyncio.get_running_loop().create_task(self._run())
        await self._ready.wait()
        # AUDIT FIX (ported): startup failures must FAIL the connect instead
        # of silently yielding a running session with user_id=None
        if self._startup_error is not None:
            raise AccountError(f"live client failed to start: {self._startup_error}")
        if self.user_id is None:
            raise AccountError("live client connected without a user (bad token?)")
        self._logger.info("live client connected as user %s", self.user_id)

    async def _run(self) -> None:
        """Run the client until it exits; capture startup errors for connect()."""
        try:
            await self._client.start(self._token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # startup failures must surface via connect()
            self._logger.error("live client terminated: %s", exc)
            self._startup_error = exc
        finally:
            self._ready.set()

    async def disconnect(self) -> None:
        """Close the underlying client; safe when never started."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as exc:  # closing a half-open client can throw
                self._logger.warning("live client close raised %s", exc)

    # ------------------------------------------------------------- messaging
    def _require_client(self) -> Any:
        """The live client, or ConnectionError when never connected."""
        if self._client is None:
            raise ConnectionError("live client is not connected")
        return self._client

    def _map_send_error(self, exc: Exception, channel_id: int) -> Optional[Exception]:
        """Translate one library send error into a domain error (or None).

        * HTTPException 429 -> ServerRateLimit (Retry-After float-parsable
          header when present, else 1.0; global flag from X-RateLimit-Global)
        * Forbidden        -> AccessDenied(account, reason)
        * NotFound         -> TargetGone("", reason)
        """
        discord = self._discord
        if discord is None or not isinstance(exc, discord.HTTPException):
            return None
        status = getattr(exc, "status", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status", None)
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if status == 429:
            raw = _header(headers, "Retry-After")
            try:
                retry_after = float(raw)
            except (TypeError, ValueError):
                retry_after = 1.0
            global_ = str(_header(headers, "X-RateLimit-Global") or "").lower() == "true"
            return ServerRateLimit(retry_after, global_)
        if status == 403 or isinstance(exc, getattr(discord, "Forbidden", ())):
            return AccessDenied(
                self._account or "",
                f"discord refused a send to channel {channel_id}: {exc}",
            )
        if status == 404 or isinstance(exc, getattr(discord, "NotFound", ())):
            return TargetGone("", f"channel {channel_id} is gone: {exc}")
        return None

    async def _send_to_channel(
        self, channel: Any, channel_id: int, content: str, *, dm: bool
    ) -> DiscordMessage:
        """Send via a resolved channel object with domain-error mapping."""
        try:
            sent = await channel.send(content)
        except Exception as exc:
            mapped = self._map_send_error(exc, channel_id)
            if mapped is not None:
                raise mapped from exc
            raise
        return DiscordMessage(
            id=getattr(sent, "id", 0),
            channel_id=channel_id,
            author_id=self.user_id or 0,
            content=content,
            # derive from the channel TYPE when possible: DM sends now arrive
            # through send_message with an already-resolved DM channel id
            dm=dm or self._is_dm_channel(channel),
        )

    def _is_dm_channel(self, channel: Any) -> bool:
        """True when the channel object is a DM channel (type-based)."""
        discord = self._discord
        if discord is None or channel is None:
            return False
        dm_type = getattr(discord, "DMChannel", None)
        return dm_type is not None and isinstance(channel, dm_type)

    async def send_message(self, channel_id: int, content: str) -> DiscordMessage:
        """Send to a channel id (cache first, fetch fallback); returns the
        framework message shape.

        Raises ServerRateLimit / AccessDenied / TargetGone for the mapped
        library failures; anything else propagates unchanged.
        """
        client = self._require_client()
        channel = client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(channel_id)
            except Exception as exc:
                mapped = self._map_send_error(exc, channel_id)
                if mapped is not None:
                    raise mapped from exc
                raise
        return await self._send_to_channel(channel, channel_id, content, dm=False)

    async def _dm_channel(self, user_id: int) -> Any:
        """Resolve (opening if needed) the DM channel object for a user."""
        client = self._require_client()
        try:
            user = await client.fetch_user(user_id)
            return user.dm_channel or await user.create_dm()
        except Exception as exc:
            mapped = self._map_send_error(exc, user_id)
            if mapped is not None:
                raise mapped from exc
            raise

    async def send_dm(self, user_id: int, content: str) -> DiscordMessage:
        """Send a DM to a user id (same error mapping as send_message)."""
        channel = await self._dm_channel(user_id)
        return await self._send_to_channel(channel, channel.id, content, dm=True)

    async def open_dm(self, user_id: int) -> int:
        """Open (or reuse) the DM channel with a user; returns its id."""
        channel = await self._dm_channel(user_id)
        return channel.id

    async def send_typing(self, channel_id: int) -> None:
        """Fire a typing indicator in a channel (anti-flag human behavior).

        Best-effort by contract: callers swallow failures — typing must
        never block or fail a send.
        """
        client = self._require_client()
        channel = client.get_channel(channel_id)
        if channel is None:
            return  # unknown channel: skip rather than fetch (API-call trim)
        await channel.trigger_typing()

    async def _resolve_message(self, channel_id: int, message_id: int) -> Any:
        """Message object by ids — cache first, fetch on miss (the ONE
        plugin-demanded fetch for button/reaction actions; §15)."""
        client = self._require_client()
        channel = client.get_channel(channel_id)
        if channel is None:
            channel = await client.fetch_channel(channel_id)
        message = channel.get_message(message_id) if hasattr(
            channel, "get_message") else None
        if message is None:
            message = await channel.fetch_message(message_id)
        return message

    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> Any:
        """React to a message (kakerareact capability; pipeline-paced)."""
        try:
            message = await self._resolve_message(channel_id, message_id)
            await message.add_reaction(emoji)
        except Exception as exc:
            mapped = self._map_send_error(exc, channel_id)
            if mapped is not None:
                raise mapped from exc
            raise

    async def click_button(self, channel_id: int, message_id: int, custom_id: str) -> Any:
        """Click a component by custom_id (kakeraloot capability).

        Uses the library's Button.click() (supported on user tokens). The
        component must still exist on the live message; a vanished custom_id
        raises TargetGone with the id in the reason.
        """
        try:
            message = await self._resolve_message(channel_id, message_id)
        except Exception as exc:
            mapped = self._map_send_error(exc, channel_id)
            if mapped is not None:
                raise mapped from exc
            raise
        for top in getattr(message, "components", None) or ():
            for component in getattr(top, "children", None) or ():
                if getattr(component, "custom_id", None) == custom_id:
                    try:
                        return await component.click()
                    except Exception as exc:
                        mapped = self._map_send_error(exc, channel_id)
                        if mapped is not None:
                            raise mapped from exc
                        raise
        from core.errors import TargetGone

        raise TargetGone("", f"custom_id {custom_id!r} not on message {message_id}")

    async def fetch_user(self, user_id: int) -> Any:
        """Fetch a user object from the library."""
        return await self._require_client().fetch_user(user_id)

    # ---------------------------------------------------------- observability
    @property
    def latency_ms(self) -> Optional[float]:
        """Gateway heartbeat latency in ms (None before READY / when NaN)."""
        client = self._client
        if client is None:
            return None
        try:
            latency = client.latency
        except Exception:
            return None
        if latency is None:
            return None
        try:
            latency = float(latency)
        except (TypeError, ValueError):
            return None
        if math.isnan(latency) or latency < 0:
            return None
        return latency * 1000.0

    def cached_snapshot(self) -> dict:
        """In-memory view only — NO network fetches (details page source).

        Uses exactly what the live client already holds in cache after READY:
        our user, guilds + their channels, open DM channels (recipient ids),
        and the heartbeat latency. ``{"available": False}`` before connect.
        """
        client = self._client
        if client is None or self.user_id is None:
            return {"available": False}
        user = client.user
        guilds = [
            {
                "id": guild.id,
                "name": guild.name,
                "channels": [
                    {"id": channel.id, "name": getattr(channel, "name", None) or ""}
                    for channel in getattr(guild, "channels", [])
                ],
            }
            for guild in getattr(client, "guilds", [])
        ]
        dms = [
            {
                "id": channel.id,
                "recipient": getattr(getattr(channel, "recipient", None), "id", None),
            }
            for channel in getattr(client, "private_channels", [])
        ]
        return {
            "available": True,
            "user": {"id": user.id, "name": str(user)},
            "guilds": guilds,
            "dm_channels": dms,
            "latency_ms": self.latency_ms,
        }


class RealClientFactory:
    """ClientFactory protocol implementation building live clients.

    The pinned factory signature is ``(name, token)`` (adapters/base.py): the
    account manager resolves the token from the account record's ref and hands
    it over. When the caller passes an empty token, the factory falls back to
    the conventional ``DISCORD_TOKEN_<NAME>`` entry of the injected
    credentials before giving up with a human-readable CredentialsError.
    """

    def __init__(
        self,
        credentials: Optional[Credentials] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._credentials = credentials
        self._logger = logger or LOG

    def __call__(self, name: str, token: str) -> RealDiscordClient:
        """Build a RealDiscordClient for one account.

        Raises CredentialsError when no token is available for the account.
        """
        if not token and self._credentials is not None:
            token = self._credentials.token_for(name) or ""
        if not token:
            raise CredentialsError(
                f"account {name!r} has no token (set DISCORD_TOKEN_{name.upper()} in .env)"
            )
        client = RealDiscordClient(token=token, logger=self._logger)
        client.account = name  # enrich AccessDenied reasons, best effort
        return client


__all__ = ["RealDiscordClient", "RealClientFactory"]
