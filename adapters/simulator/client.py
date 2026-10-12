"""SimClient — the simulated half of the adapter boundary (ported from the
audited ``core/simulator.py`` FakeClient, adapted to adapters.base).

One SimClient simulates ONE logged-in account living inside a
:class:`adapters.simulator.world.SimWorld`. It implements the full
:class:`adapters.base.DiscordClient` protocol so the account manager,
scheduler and plugins cannot tell it apart from the real client.

Behaviour highlights (audit-proven unless noted):
  * connect/disconnect are idempotent — no duplicate world subscriptions,
  * ``drop(reason)`` simulates an UNEXPECTED loss and fires on_disconnect,
  * ``send_message``/``send_dm`` mirror the real error contract:
    ConnectionError when offline, :class:`core.errors.ServerRateLimit` for a
    queued one-shot 429 (world-owned queue), :class:`core.errors.TargetGone`
    for a vanished channel,
  * ``cached_snapshot`` filters DM channels to pairs involving THIS account
    (audit fix: the world-level view used to leak other accounts' DMs),
  * ``latency_ms`` is synthetic but deterministic per client (seedable).
"""
from __future__ import annotations

import random
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional

from adapters.base import DiscordMessage
from adapters.simulator.world import SimWorld, WorldMessage, to_discord_message
from core.errors import ServerRateLimit, TargetGone

#: synthetic latency band (milliseconds) — heartbeat stand-in
LATENCY_MIN_MS = 40.0
LATENCY_MAX_MS = 90.0


class SimClient:
    """DiscordClient protocol implementation over a SimWorld (per account)."""

    def __init__(
        self,
        world: SimWorld,
        user_id: int,
        *,
        seed: Optional[int] = None,
        fail_connect: bool = False,
    ) -> None:
        """Bind the client to a world as one user.

        Args:
            world: the shared simulated Discord world.
            user_id: synthetic user id this client logs in as.
            seed: deterministic seed for the synthetic latency sequence
                (tests pin it; None uses OS entropy).
            fail_connect: when True, connect() always raises ConnectionError
                (failure-path testing of the account supervisor).
        """
        self._world = world
        self.user_id = user_id
        self.on_message: Optional[Callable[[DiscordMessage], None]] = None
        self.on_disconnect: Optional[Callable[[str], None]] = None
        self.on_reaction: Optional[Callable[..., None]] = None
        self.on_message_edit: Optional[Callable[[DiscordMessage], None]] = None
        self.on_message_delete: Optional[Callable[..., None]] = None
        self.connected = False
        self.fail_connect = fail_connect
        self._rng = random.Random(seed)
        #: typing indicators fired (observable for anti-flag tests)
        self.typing_calls: list = []
        #: REST-touching DM opens (observable for API-trim tests)
        self.open_dm_calls: list = []
        #: button clicks performed (observable for capability tests)
        self.button_clicks: list = []

    # -------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        """Subscribe to the world (idempotent — no duplicate subscriptions).

        Raises ConnectionError in fail_connect mode (simulated bad start).
        """
        if self.fail_connect:
            raise ConnectionError(
                f"simulated connect failure for user {self.user_id} (fail_connect=True)"
            )
        if self.connected:  # AUDIT FIX: idempotent — no duplicate subscriptions
            return
        self._world.subscribers.append(self)
        self.connected = True

    async def disconnect(self) -> None:
        """Unsubscribe from the world (idempotent, never fires callbacks)."""
        if self in self._world.subscribers:
            self._world.subscribers.remove(self)
        self.connected = False

    def drop(self, reason: str = "simulated network drop") -> None:
        """Simulate an UNEXPECTED connection loss (fires on_disconnect)."""
        if not self.connected:
            return
        if self in self._world.subscribers:
            self._world.subscribers.remove(self)
        self.connected = False
        if self.on_disconnect is not None:
            self.on_disconnect(reason)

    def delete_channel(self, channel_id: int) -> bool:
        """Simulate a channel deletion (delegates to the world)."""
        return self._world.channel_gone(channel_id)

    # -------------------------------------------------------------- messaging
    def _guard_connection(self) -> None:
        """Raise ConnectionError when offline — checked FIRST.

        GUARD ORDER (audit round 3 fix, live parity): the live adapter's
        ``_require_client()`` runs before ANY channel state is resolved, so
        a send from a disconnected client classifies as ConnectionError
        even when the target channel is also gone. The sim used to check
        the channel first, so its retry-classification tests lied about
        which failure live code would see.
        """
        if not self.connected:
            raise ConnectionError("sim client not connected")

    def _guard_send(self, channel_id: Optional[int]) -> Optional[ServerRateLimit]:
        """Common send pre-checks: connection + queued one-shot server limit.

        Raises ConnectionError when not connected; returns a pending
        ServerRateLimit (already consumed + recorded by the world) when one
        matches this client/channel.
        """
        if not self.connected:
            raise ConnectionError("sim client not connected")
        return self._world.take_server_limit(self.user_id, channel_id)

    def _guard_channel(self, channel_id: int) -> None:
        """Raise TargetGone for a channel the world no longer knows."""
        if channel_id not in self._world.channels:
            raise TargetGone("", f"channel {channel_id} vanished")

    async def send_message(self, channel_id: int, content: str) -> DiscordMessage:
        """Deliver a guild-channel message through the world.

        Raises ConnectionError when offline, ServerRateLimit for a queued
        one-shot 429, TargetGone when the channel vanished — checked in
        that order, exactly like the live adapter (session first, channel
        resolution second, rate state last: a one-shot 429 is never
        consumed on a send whose target is already gone).
        """
        self._guard_connection()
        self._guard_channel(channel_id)
        limit = self._guard_send(channel_id)
        if limit is not None:
            raise limit
        sent = self._world.deliver(channel_id, self.user_id, content)
        return self._as_discord_message(sent, dm=False)

    async def send_dm(self, user_id: int, content: str) -> DiscordMessage:
        """Deliver a DM to a user through the world's deterministic DM channel."""
        self._guard_connection()
        channel_id = self._world.dm_channel_for(self.user_id, user_id)
        self._guard_channel(channel_id)
        limit = self._guard_send(channel_id)
        if limit is not None:
            raise limit
        sent = self._world.deliver(channel_id, self.user_id, content, dm=True)
        return self._as_discord_message(sent, dm=True)

    async def send_typing(self, channel_id: int) -> None:
        """Record a typing indicator (no world effect; observable in tests).

        Live parity (audit round 3): ConnectionError when offline, and a
        silent skip for channels the session does not know (the live
        adapter never fetches a channel just to type into it) — an
        offline client no longer leaves phantom typing evidence behind.
        """
        self._guard_connection()
        if channel_id not in self._world.channels:
            return
        self.typing_calls.append((channel_id, time.monotonic()))

    async def open_dm(self, user_id: int) -> int:
        """The (deterministic, symmetric) DM channel id for a user pair.

        Live parity: ConnectionError when offline (the live adapter
        resolves the session before any REST touch, so a disconnected
        client never opens — or records — a DM).
        """
        self._guard_connection()
        self.open_dm_calls.append(int(user_id))  # observable for API-trim tests
        return self._world.dm_channel_for(self.user_id, user_id)

    async def fetch_user(self, user_id: int) -> Optional[SimpleNamespace]:
        """None when the world does not know the user, else id/name attrs."""
        if user_id not in self._world.users:
            return None
        return SimpleNamespace(id=user_id, name=self._world.users[user_id])

    # ---------------------------------------------------------- observability
    @property
    def latency_ms(self) -> float:
        """Synthetic heartbeat latency: deterministic 40-90ms per seed."""
        return round(self._rng.uniform(LATENCY_MIN_MS, LATENCY_MAX_MS), 1)

    def cached_snapshot(self) -> dict:
        """World-backed equivalent of the live client's in-memory cache.

        AUDIT FIX (ported): DM channels are filtered to pairs involving THIS
        account — the world-level view used to leak other accounts' DMs.
        Shape per adapters.base (incl. latency_ms); ``{"available": False}``
        when not connected.
        """
        if not self.connected:
            return {"available": False}
        guild_channels = [
            {"id": cid, "name": name}
            for cid, name in self._world.channels.items()
            if not self._world.dm_channels.get(cid, False)
        ]
        # DM entries carry EXACTLY the live contract {id, recipient} — the
        # extra "name" key was sim-only shape drift (audit round 3)
        dm_channels = [
            {
                "id": cid,
                "recipient": self._dm_recipient(cid),
            }
            for cid in self._world.channels
            if self._world.dm_channels.get(cid, False)
            and any(self.user_id in pair for pair in self._world.dm_pairs_of(cid))
        ]
        return {
            "available": True,
            "user": {"id": self.user_id, "name": self._world.users.get(self.user_id, "?")},
            "guilds": [{"id": 0, "name": "sim-guild", "channels": guild_channels}],
            "dm_channels": dm_channels,
            "latency_ms": self.latency_ms,
        }

    def _dm_recipient(self, channel_id: int) -> Optional[int]:
        """The OTHER member of a DM channel pair, as seen by this account."""
        for pair in self._world.dm_pairs_of(channel_id):
            other = pair[0] if pair[1] == self.user_id else pair[1]
            return other
        return None

    # --------------------------------------------------------------- internal
    @staticmethod
    def _as_discord_message(sent: WorldMessage, *, dm: bool) -> DiscordMessage:
        """World message -> protocol message shape."""
        return DiscordMessage(
            id=sent.id,
            channel_id=sent.channel_id,
            author_id=sent.author_id,
            content=sent.content,
            dm=dm,
        )

    def receive(self, message: WorldMessage) -> None:
        """World push -> framework callback (ignores our own messages).

        Rich boundary: whatever the world row carries (embeds, components,
        attachments, reactions, author/guild, reference) crosses into the
        DTO untouched — same contract as the live adapter, and now the
        SAME builder the world uses for edit events (to_discord_message)
        so every sim crossing is shape-identical.

        DM parity: a DM is delivered only to the two members of its pair
        (non-creating world lookup) — every client used to receive every
        DM, so account B's on_message fired for pages between A and Mudae.
        """
        if message.author_id == self.user_id:
            return
        if message.dm:
            pairs = self._world.dm_pairs_of(message.channel_id)
            if pairs and self.user_id not in pairs[0]:
                return  # someone else's DM conversation
        if self.on_message is not None:
            self.on_message(to_discord_message(self._world, message))

    def reaction_event(
        self, kind: str, message_id: int, channel_id: int,
        guild_id: "int | None", user_id: int, emoji: str, raw: Any,
    ) -> None:
        """World reaction dispatch -> on_reaction. The world pushes EVERY
        reaction (gateway truth, reactor included); the CLIENT drops the
        session's own reactions — mirroring the live adapter's
        _publish_raw_reaction self-filter (adapters/discord/client.py)."""
        if self.on_reaction is None or user_id == self.user_id:
            return
        self.on_reaction(
            kind, message_id, channel_id, guild_id, user_id, emoji, raw)

    # ------------------------------------------------- capability actions
    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> Any:
        """React via the world (echoes a ReactionAdded to other subscribers).

        Raises ConnectionError when offline, ServerRateLimit for a queued
        one-shot 429 (consumed exactly like send_message — an armed limit
        now BLOCKS the reaction instead of being silently spent by an
        unrelated later message), TargetGone for a vanished channel or
        message — matching the live adapter's contract (a generic error
        cannot be reconstructed into FlowBlocked by the services layer).
        """
        if not self.connected:
            raise ConnectionError("client not connected")
        if channel_id not in self._world.channels:
            raise TargetGone("", f"channel {channel_id} vanished")
        if not any(
            m.id == message_id
            for messages in self._world.history.values()
            for m in messages
        ):
            # resolve the target BEFORE consuming the one-shot limit (a
            # 429 is never spent on a send whose target is already gone)
            raise TargetGone("", f"message {message_id} vanished")
        limit = self._world.take_server_limit(self.user_id, channel_id)
        if limit is not None:
            raise limit
        try:
            self._world.react(self.user_id, message_id, emoji, kind="add")
        except KeyError:
            raise TargetGone("", f"message {message_id} vanished") from None
        return None

    async def click_button(self, channel_id: int, message_id: int, custom_id: str) -> Any:
        """Click a button the world still holds (TargetGone when vanished).

        ServerRateLimit for a queued one-shot 429, consumed exactly like
        send_message — only after the target (message + custom_id) is
        resolved, never on a dead click.
        """
        from adapters.model import DiscordComponent

        if not self.connected:
            raise ConnectionError("client not connected")
        for messages in self._world.history.values():
            for message in messages:
                if message.id == message_id:
                    for top in message.components:
                        for component in (top.walk() if isinstance(
                                top, DiscordComponent) else ()):
                            if component.custom_id == custom_id:
                                limit = self._world.take_server_limit(
                                    self.user_id, channel_id)
                                if limit is not None:
                                    raise limit
                                self.button_clicks.append(
                                    (message_id, custom_id))
                                return None
                    raise TargetGone(
                        "", f"custom_id {custom_id!r} not on message {message_id}")
        raise TargetGone("", f"message {message_id} vanished")


__all__ = ["SimClient", "LATENCY_MIN_MS", "LATENCY_MAX_MS"]
