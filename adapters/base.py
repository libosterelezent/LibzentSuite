"""Discord client abstraction (spec §9): ONE interface, two implementations.

    DiscordClient (protocol)
        /                \
  Real Client          Simulator
  (adapters/discord)  (adapters/simulator)

The framework (account manager, scheduler sender hook, plugins) only ever
talks to this protocol — never to a concrete library — which is what makes
full offline simulation possible.

Implementations must provide:
  * lifecycle : connect(), disconnect(), connected state
  * messaging : send_message(channel_id, content), send_dm(user_id, content),
                open_dm(user_id) -> channel_id, fetch_user(user_id)
  * observability: user_id (once connected), latency_ms (gateway heartbeat),
                cached_snapshot() — in-memory view only, NEVER a network fetch
  * callbacks : on_message(DiscordMessage), on_disconnect(reason) — assigned
                by the account manager before connect()

Server-provided rate limits: when Discord answers 429 the client raises
core.errors.ServerRateLimit(retry_after, global_). The scheduler reports it
to the rate limit manager and retries after backoff — the framework always
respects server-provided rate-limit information.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

# RICH MODEL (adapters/model.py) — the full stable projection (embeds,
# components, attachments, reactions, reference, author/channel/guild,
# created_at, runtime-only raw escape hatch). Re-exported here so every
# existing ``from adapters.base import DiscordMessage`` keeps working.
from .model import (  # noqa: F401 — compatibility re-exports
    DiscordAttachment,
    DiscordChannel,
    DiscordComponent,
    DiscordEmbed,
    DiscordEmbedField,
    DiscordMessage,
    DiscordMessageReference,
    DiscordReaction,
    DiscordUser,
    convert_message,
)




@runtime_checkable
class DiscordClient(Protocol):
    """The minimal surface every adapter must implement."""

    #: Discord user id of the logged-in account (None until connected)
    user_id: Optional[int]
    #: gateway heartbeat latency in milliseconds (None when unknown)
    latency_ms: Optional[float]
    #: callbacks assigned by the account manager BEFORE connect()
    on_message: Optional[Callable[[DiscordMessage], None]]
    on_disconnect: Optional[Callable[[str], None]]
    #: OPTIONAL rich-event callbacks (None on clients that lack the event):
    #: on_reaction(kind: "add"|"remove", message_id, channel_id, guild_id,
    #:              user_id, emoji: str, raw)
    #: on_message_edit(message: DiscordMessage)   (post-edit, raw=library msg)
    #: on_message_delete(message_id, channel_id, guild_id, raw)
    on_reaction: Optional[Callable[..., None]]
    on_message_edit: Optional[Callable[[DiscordMessage], None]]
    on_message_delete: Optional[Callable[..., None]]

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def send_message(self, channel_id: int, content: str) -> DiscordMessage: ...

    async def send_dm(self, user_id: int, content: str) -> DiscordMessage: ...

    async def open_dm(self, user_id: int) -> int: ...

    async def fetch_user(self, user_id: int) -> Any: ...

    # ----- OPTIONAL capability actions (plugin-demanded, pipeline-paced) ------
    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> Any:
        """React to a message (kakerareact capability). Raises the shared
        domain errors (ServerRateLimit/AccessDenied/TargetGone)."""

    async def click_button(self, channel_id: int, message_id: int, custom_id: str) -> Any:
        """Click a component by custom_id (kakeraloot capability). Resolves
        the message on demand (plugin-requested fetch) and clicks the
        matching button; raises the shared domain errors."""

    def cached_snapshot(self) -> dict:
        """In-memory view: {"available": bool, "user": {"id","name"},
        "latency_ms": float|None, "guilds": [{"id","name",
        "channels": [{"id","name"}...]}...], "dm_channels": [{"id","recipient"}...]}.

        REAL-TIME RULE: builds from what the client already holds (post-READY
        cache). Performs NO network calls — the account details page draws
        exclusively from this.
        """


@runtime_checkable
class ClientFactory(Protocol):
    """Builds a DiscordClient for one account (real or simulated)."""

    def __call__(self, name: str, token: str) -> Any: ...


__all__ = ["DiscordClient", "DiscordMessage", "ClientFactory"]
