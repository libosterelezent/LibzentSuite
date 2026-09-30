"""Live Discord adapter.

Bridges the framework's DiscordClientProtocol to a real selfbot client library.
The import is intentionally LAZY (inside connect()) so tests, simulations and
CLI dry-runs never require the library. Point REQUIREMENTS at whichever fork
you run (e.g. discord.py-self); only the small surface below is used:

  discord.Client(intents=...), client.start(token), client.close(),
  on_ready/on_message callbacks, get_channel/fetch_channel(...).send(...),
  fetch_user(...).dm_channel / create_dm().

NOTE: selfbots violate Discord ToS; keep rate rules conservative.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from .account import AccountConfig, DiscordMessage
from .credentials import Credentials

LOG = logging.getLogger("slaughtermudae.adapter")


class AdapterNotAvailable(RuntimeError):
    """Raised when the live Discord library is not installed."""


class RealDiscordClient:
    """DiscordClientProtocol implementation over a lazily imported live library."""

    def __init__(self, token: str, logger: Optional[logging.Logger] = None) -> None:
        self._token = token
        self._discord: Any = None
        self._client: Any = None
        self._ready = asyncio.Event()
        self._startup_error: Optional[Exception] = None
        self._logger = logger or LOG
        self.on_message: Optional[Callable[[DiscordMessage], None]] = None
        self.on_disconnect: Optional[Callable[[str], None]] = None
        self.user_id: Optional[int] = None

    # ------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        """Import the library, wire callbacks, start the client, await ready."""
        try:
            import discord  # type: ignore[import-untyped]  # discord.py-self fork
        except ImportError as exc:
            raise AdapterNotAvailable(
                "live mode requires a user-account-capable discord client library "
                "(e.g. 'pip install discord.py-self')"
            ) from exc
        self._discord = discord
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

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
            if self.on_message is not None and message.author.id != self.user_id:
                self.on_message(
                    DiscordMessage(
                        id=message.id,
                        channel_id=message.channel.id,
                        author_id=message.author.id,
                        content=message.content or "",
                        dm=isinstance(message.channel, self._discord.DMChannel),
                    )
                )

        asyncio.get_running_loop().create_task(self._run())
        await self._ready.wait()
        # AUDIT FIX: login/startup failures now FAIL the connect instead of
        # silently yielding a RUNNING session with user_id=None
        if getattr(self, "_startup_error", None) is not None:
            raise AdapterNotAvailable(f"live client failed to start: {self._startup_error}")
        if self.user_id is None:
            raise AdapterNotAvailable("live client connected without a user (bad token?)")
        self._logger.info("live client connected as user %s", self.user_id)

    async def _run(self) -> None:
        try:
            await self._client.start(self._token)
        except Exception as exc:  # startup failures must surface via connect()
            self._logger.error("live client terminated: %s", exc)
            self._startup_error = exc
        finally:
            self._ready.set()

    def cached_snapshot(self) -> dict:
        """In-memory view only — NO network fetches (details page data source).

        Uses exactly what the live client already holds in cache after READY:
        our user, guilds + their channels, and open DM channels.
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
        }

    async def disconnect(self) -> None:
        """Close the underlying client; safe if never started."""
        if self._client is not None:
            await self._client.close()

    # ------------------------------------------------------------- messaging
    async def send_message(self, channel_id: int, content: str) -> DiscordMessage:
        """Send to a channel id; returns the framework message shape."""
        channel = self._client.get_channel(channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(channel_id)
        sent = await channel.send(content)
        return DiscordMessage(
            id=sent.id, channel_id=channel_id, author_id=self.user_id or 0, content=content
        )

    async def fetch_user(self, user_id: int) -> Any:
        """Fetch a user object (None if not resolvable)."""
        return await self._client.fetch_user(user_id)

    async def open_dm(self, user_id: int) -> int:
        """Open (or reuse) the DM channel with a user; returns channel id."""
        user = await self._client.fetch_user(user_id)
        channel = user.dm_channel or await user.create_dm()
        return channel.id


class RealClientFactory:
    """client_factory for the container: builds a live client from credentials."""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or LOG

    def __call__(self, config: AccountConfig, credentials: Credentials) -> RealDiscordClient:
        """Create a RealDiscordClient; raises CredentialsError if token missing."""
        token = credentials.get(config.effective_token_ref())
        if not token:
            from .credentials import CredentialsError

            raise CredentialsError(
                f"account {config.name!r} has no token; set {config.effective_token_ref()} in .env"
            )
        return RealDiscordClient(token=token, logger=self._logger)
