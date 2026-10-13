"""SimWorld — a complete in-memory Discord world (ported from the audited
``core/simulator.py`` FakeWorld).

Everything here is SYNTHETIC: fixture user/channel ids are module constants
and never correspond to real Discord ids (real ones live in .env only).

The world owns:
  * users / channels maps and a dm flag map,
  * deterministic, SYMMETRIC DM channel ids with a collision fallback
    (audit round 1 fix: two pairs must never merge into one channel),
  * per-channel history plus an append-only journal that survives channel
    deletion (scenario checks need post-mortem evidence),
  * a configurable scripted responder: an exact command string (e.g.
    ``"$slud-s"``) triggers a list of pages delivered via DM to the
    requesting user, one page at a time with a configurable per-page delay,
  * ``wait_for_quiet`` with REAL quiet-window semantics (audit fix: return
    once `quiet` seconds of silence passed, `limit` is only a hard cap),
  * a one-shot server-limit queue (``server_limit_next``): the NEXT matching
    send raises :class:`core.errors.ServerRateLimit` instead of delivering,
    consumed exactly once,
  * ``channel_gone``: remove a channel so later sends raise TargetGone and
    cached snapshots stop listing it.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from core.errors import ServerRateLimit

LOG = logging.getLogger("slaughtermudae.simulator")

# --- synthetic fixture ids (NOT real Discord ids; real ones live in .env) ---
SYNTHETIC_OWNER_ID = 1001
SYNTHETIC_ALPHA_ID = 2001
SYNTHETIC_BETA_ID = 2002
SYNTHETIC_GUILD_CHANNEL = 500
SYNTHETIC_MUDAE_ID = 999_999

#: custom-emoji repr "<:name:id>" / "<a:name:id>" -> (bare name, id); the
#: inverse of adapters.model._emoji_repr (unicode emoji pass through)
_CUSTOM_EMOJI_RE = re.compile(r"^<a?:(.+):(\d+)>$")


def _parse_emoji(text: str) -> Tuple[str, Optional[int]]:
    """Emoji string -> (bare name, custom emoji id or None)."""
    match = _CUSTOM_EMOJI_RE.match(text or "")
    if match:
        return match.group(1), int(match.group(2))
    return text, None


def to_discord_message(world: "SimWorld", message: "WorldMessage"):
    """WorldMessage -> rich frozen DTO (the sim's convert_message).

    ONE builder for every crossing — live deliveries (SimClient.receive)
    and edit events (SimWorld.edit_message) — so the sim's rich boundary
    matches the live adapter's :func:`adapters.model.convert_message`
    projection: author/channel/guild, embeds, components, attachments,
    reactions and the reply reference all cross untouched.

    Fidelity parity with the live projection: the scripted responder is a
    BOT (author.bot True for the responder's deliveries — bot-gate plugins
    behave the same in sim and live; user authors stay False),
    created_at is a tz-aware UTC datetime built from the world timestamp
    (the live converter passes the library's aware datetime through), and
    display_name mirrors the author name.
    """
    from adapters.base import DiscordMessage
    from adapters.model import DiscordChannel, DiscordMessageReference, DiscordUser

    return DiscordMessage(
        id=message.id,
        channel_id=message.channel_id,
        author_id=message.author_id,
        content=message.content,
        dm=message.dm,
        guild_id=message.guild_id,
        author=DiscordUser(
            id=message.author_id,
            name=message.author_name,
            display_name=message.author_name,
            bot=(world.responder_id is not None
                 and message.author_id == world.responder_id),
        ),
        channel=DiscordChannel(
            id=message.channel_id,
            name=world.channels.get(message.channel_id, ""),
            type="private" if message.dm else "text",
            guild_id=message.guild_id,
        ),
        embeds=tuple(message.embeds),
        components=tuple(message.components),
        attachments=tuple(message.attachments),
        reactions=tuple(message.reactions),
        reference=(
            DiscordMessageReference(
                message_id=message.reference_message_id,
                channel_id=message.channel_id,
                guild_id=message.guild_id)
            if message.reference_message_id else None
        ),
        created_at=datetime.fromtimestamp(message.ts, tz=timezone.utc),
    )


@dataclass
class WorldMessage:
    """One message that exists in the world (delivered + timestamped).

    Rich payloads (embeds/components/attachments/reactions as
    adapters.model DTO tuples, author_name, guild_id, reference ids) are
    optional keyword arguments forwarded by :meth:`deliver` — so
    scenarios can exercise the full rich boundary exactly like live
    traffic.
    """

    id: int
    channel_id: int
    author_id: int
    content: str
    dm: bool
    ts: float = field(default_factory=time.time)
    author_name: str = ""
    guild_id: "int | None" = None
    embeds: tuple = ()
    components: tuple = ()
    attachments: tuple = ()
    reactions: tuple = ()
    reference_message_id: "int | None" = None


@dataclass
class _PendingLimit:
    """A queued one-shot 429 (see SimWorld.server_limit_next)."""

    channel_id: Optional[int]      # None matches any channel
    user_id: Optional[int]         # None matches any sending client
    retry_after: float
    global_: bool


class SimWorld:
    """In-memory Discord: users, channels, DM channels, history, responders."""

    def __init__(
        self,
        *,
        responder_id: Optional[int] = None,
        responses: Optional[Dict[str, List[str]]] = None,
        reply_delay: float = 0.01,
    ) -> None:
        """Create an empty world.

        Args:
            responder_id: user id of the scripted responder (None disables it).
            responses: exact command string -> list of DM pages it answers with.
            reply_delay: seconds to wait before the first page and between pages.
        """
        self.users: Dict[int, str] = {}
        self.channels: Dict[int, str] = {}
        self.dm_channels: Dict[int, bool] = {}
        self.history: Dict[int, List[WorldMessage]] = {}
        #: append-only record of every DELIVERED message (survives deletion)
        self.journal: List[WorldMessage] = []
        #: observability for consumed one-shot server limits (ts + details)
        self.blocked_sends: List[dict] = []
        #: channel_id -> deletion timestamp (evidence for abort checks)
        self.gone_channels: Dict[int, float] = {}
        self._dm_pairs: Dict[Tuple[int, int], int] = {}
        self._reverse_dm: Dict[int, Tuple[int, int]] = {}
        self._dm_seq = 0
        self._next_id = 1
        self.subscribers: List[Any] = []
        self.responder_id = responder_id
        self.responses: Dict[str, List[str]] = dict(responses or {})
        self.reply_delay = reply_delay
        self._responder_tasks: set = set()  # strong refs (create_task hazard)
        #: queued one-shot server limits (world-owned, per-client intake)
        self._pending_limits: List[_PendingLimit] = []

    # -------------------------------------------------------------- topology
    def add_user(self, user_id: int, name: str) -> None:
        """Register a user id (idempotent; last name wins)."""
        self.users[user_id] = name

    def add_channel(self, channel_id: int, name: str, dm: bool = False) -> None:
        """Register a channel id (idempotent; last name wins)."""
        self.channels[channel_id] = name
        self.dm_channels[channel_id] = dm
        self.history.setdefault(channel_id, [])
        self.gone_channels.pop(channel_id, None)

    def channel_exists(self, channel_id: int) -> bool:
        """True while the channel still exists in the world."""
        return channel_id in self.channels

    def channel_gone(self, channel_id: int) -> bool:
        """Simulate a channel deletion.

        The world forgets the channel (history included): later sends to it
        must raise :class:`core.errors.TargetGone` at the client layer, and
        cached snapshots stop listing it. Deletion time is recorded in
        ``gone_channels`` for scenario post-mortems. Returns True when the
        channel existed.
        """
        if channel_id not in self.channels:
            return False
        was_dm = self.dm_channels.get(channel_id, False)
        self.channels.pop(channel_id, None)
        self.dm_channels.pop(channel_id, None)
        self.history.pop(channel_id, None)
        self.gone_channels[channel_id] = time.time()
        if was_dm:
            # deliberate divergence from live Discord: the pair->channel
            # mapping is RETAINED, so this DM stays unsendable forever
            # (dm_channel_for keeps returning the dead id). Loud on purpose.
            LOG.warning(
                "dm channel %s deleted: pair mapping retained - DMs between "
                "this pair stay unsendable (deliberate sim divergence from "
                "live Discord, where DM channels resurrect)",
                channel_id,
            )
        return True

    def dm_channel_for(self, user_a: int, user_b: int) -> int:
        """Deterministic, symmetric DM channel id for a user pair.

        AUDIT FIX (ported): collision-checked — if the hash-style id already
        belongs to a different pair (or a guild channel), fall back to a
        sequential id instead of merging two conversations.
        """
        pair = (min(user_a, user_b), max(user_a, user_b))
        if pair not in self._dm_pairs:
            channel_id = 1_000_000 + pair[0] * 7919 + pair[1]
            while channel_id in self._reverse_dm or channel_id in self.channels:
                self._dm_seq += 1
                channel_id = 2_000_000_000 + self._dm_seq
            self._dm_pairs[pair] = channel_id
            self._reverse_dm[channel_id] = pair
            self.add_channel(channel_id, f"dm-{pair[0]}-{pair[1]}", dm=True)
        return self._dm_pairs[pair]

    def peek_dm_channel(self, user_a: int, user_b: int) -> Optional[int]:
        """Non-creating DM channel lookup: the pair's channel id, or None
        when the pair never opened one. For post-mortem checks that must
        not mutate the world (dm_channel_for CREATES the channel)."""
        pair = (min(user_a, user_b), max(user_a, user_b))
        return self._dm_pairs.get(pair)

    def add_response(self, command: str, pages: List[str]) -> None:
        """Register a scripted response: exact command string -> DM pages."""
        self.responses[command] = list(pages)

    def dm_pairs_of(self, channel_id: int) -> List[Tuple[int, int]]:
        """The (one) user pair behind a DM channel id (per-account filtering)."""
        pair = self._reverse_dm.get(channel_id)
        return [pair] if pair else []

    # --------------------------------------------------------- server limits
    def server_limit_next(
        self,
        channel_id: Optional[int] = None,
        retry_after: float = 1.0,
        global_: bool = False,
        user_id: Optional[int] = None,
    ) -> None:
        """Queue a one-shot server 429 for the NEXT matching send.

        The next send whose channel (and, when given, sending user) matches
        raises :class:`core.errors.ServerRateLimit` ``(retry_after, global_)``
        instead of delivering; the entry is consumed exactly once.

        Args:
            channel_id: restrict to one channel id (None = any channel).
            retry_after: seconds Discord would ask us to wait.
            global_: whether the simulated limit is account-wide.
            user_id: restrict to one sending client's user id (None = any).
        """
        self._pending_limits.append(
            _PendingLimit(
                channel_id=channel_id,
                user_id=user_id,
                retry_after=max(0.0, float(retry_after)),
                global_=bool(global_),
            )
        )

    def take_server_limit(
        self, user_id: int, channel_id: Optional[int]
    ) -> Optional[ServerRateLimit]:
        """Consume the first matching pending one-shot limit, if any.

        Matching rules: a queued entry with ``channel_id=None`` matches any
        channel and vice versa; same for the user. When an entry matches it
        is REMOVED (one-shot) and recorded in ``blocked_sends`` (with ts) for
        scenario timing checks. Returns the ServerRateLimit to raise, or None.
        """
        for index, pending in enumerate(self._pending_limits):
            if pending.user_id is not None and pending.user_id != user_id:
                continue
            if pending.channel_id is not None and pending.channel_id != channel_id:
                continue
            del self._pending_limits[index]
            limit = ServerRateLimit(pending.retry_after, pending.global_)
            self.blocked_sends.append(
                {
                    "ts": time.time(),
                    "user_id": user_id,
                    "channel_id": channel_id,
                    "retry_after": limit.retry_after,
                    "global_": limit.global_,
                }
            )
            return limit
        return None

    # ---------------------------------------------------------------- traffic
    def deliver(
        self, channel_id: int, author_id: int, content: str, dm: Optional[bool] = None,
        *, author_name: str = "", guild_id: "int | None" = None,
        embeds: tuple = (), components: tuple = (), attachments: tuple = (),
        reactions: tuple = (), reference_message_id: "int | None" = None,
    ) -> WorldMessage:
        """Append a message to the channel and push it to every subscriber.

        Rich payloads are adapters.model DTO tuples passed through to the
        boundary conversion untouched. Raises KeyError for an unknown (or
        deleted) channel — the client layer translates that into TargetGone.
        """
        if channel_id not in self.channels:
            raise KeyError(f"unknown channel {channel_id}")
        if dm is None:
            dm = self.dm_channels.get(channel_id, False)
        message = WorldMessage(
            self._next_id, channel_id, author_id, content, dm,
            author_name=author_name, guild_id=guild_id, embeds=embeds,
            components=components, attachments=attachments,
            reactions=reactions, reference_message_id=reference_message_id,
        )
        self._next_id += 1
        self.history[channel_id].append(message)
        self.journal.append(message)
        for client in list(self.subscribers):
            try:
                client.receive(message)
            except Exception:
                LOG.exception(
                    "world subscriber %r failed on message %s", client, message.id)
        self._maybe_auto_respond(message)
        return message

    def react(self, user_id: int, message_id: int, emoji: str,
              kind: str = "add") -> None:
        """Push a reaction event for a known message to every subscriber
        (mirrors the gateway, which dispatches reaction events to every
        connected session INCLUDING the reactor; SimClient.reaction_event
        applies the live adapter's self-filter for its own reactions).

        'add' increments (or creates) the aggregate entry, 'remove'
        decrements (or deletes) it; the raw payload's count is recomputed
        AFTER the mutation so subscribers never see stale counts, and
        ``me`` is computed per subscriber (True for the reactor).
        """
        for channel_id, messages in self.history.items():
            for message in messages:
                if message.id == message_id:
                    if kind == "add":
                        from adapters.model import DiscordReaction

                        name, emoji_id = _parse_emoji(emoji)
                        for index, existing in enumerate(message.reactions):
                            if existing.emoji == emoji:
                                # rebuild, never mutate: the frozen DTO may
                                # already sit in a plugin's DiscordMessage
                                # (replace keeps id/me, unlike a fresh build)
                                replacement = dataclasses.replace(
                                    existing, count=existing.count + 1)
                                message.reactions = (
                                    message.reactions[:index]
                                    + (replacement,)
                                    + message.reactions[index + 1:])
                                break
                        else:
                            message.reactions = message.reactions + (
                                DiscordReaction(
                                    emoji=emoji, name=name, id=emoji_id,
                                    count=1),)
                    elif kind == "remove":
                        for index, existing in enumerate(message.reactions):
                            if existing.emoji != emoji:
                                continue
                            if existing.count <= 1:
                                message.reactions = (
                                    message.reactions[:index]
                                    + message.reactions[index + 1:])
                            else:
                                replacement = dataclasses.replace(
                                    existing, count=existing.count - 1)
                                message.reactions = (
                                    message.reactions[:index]
                                    + (replacement,)
                                    + message.reactions[index + 1:])
                            break
                    # RAW PARITY (live pushes ``(reaction, user)`` handles,
                    # never None): synthetic library-shaped stand-ins built
                    # from what the world already knows — no fetch. The
                    # count is read AFTER the mutation above, and me is
                    # per subscriber (the reactor's own view is True).
                    count = 0
                    for reaction in message.reactions:
                        if reaction.emoji == emoji:
                            count = reaction.count
                    user_ns = SimpleNamespace(
                        id=user_id, name=self.users.get(user_id, ""))
                    for client in list(self.subscribers):
                        _sees = getattr(client, "can_see", None)
                        if _sees is not None and not _sees(channel_id):
                            continue   # foreign DM pair — invisible live
                        handler = getattr(client, "reaction_event", None)
                        if handler is None:
                            handler = getattr(client, "on_reaction", None)
                        if handler is None:
                            continue
                        reaction_ns = SimpleNamespace(
                            emoji=emoji, count=count,
                            me=(getattr(client, "user_id", None) == user_id),
                            message_id=message_id, channel_id=channel_id)
                        try:
                            handler(
                                kind, message_id, channel_id,
                                message.guild_id,
                                user_id, emoji, (reaction_ns, user_ns),
                            )
                        except Exception:
                            LOG.exception(
                                "world subscriber %r failed on reaction %s",
                                client, message_id)
                    return
        raise KeyError(f"unknown message {message_id}")

    def edit_message(self, message_id: int, content: str) -> WorldMessage:
        """Rewrite a message's content and push an edit event to subscribers."""
        for messages in self.history.values():
            for message in messages:
                if message.id == message_id:
                    message.content = content
                    dto = self._to_dto(message)  # frozen DTO boundary: live
                    for client in list(self.subscribers):  # adapters push the
                        # self-filter: live adapters never echo your own
                        # edits/deletes back as events (fidelity parity);
                        # DM edits are visible ONLY to pair members (an
                        # unrelated account's gateway never sees the channel)
                        _sees = getattr(client, "can_see", None)
                        if (client.user_id != message.author_id
                                and client.on_message_edit is not None
                                and (_sees is None
                                     or _sees(message.channel_id))):
                            try:
                                client.on_message_edit(dto)
                            except Exception:
                                LOG.exception(
                                    "world subscriber %r failed on edit %s",
                                    client, message_id)
                    return message
        raise KeyError(f"unknown message {message_id}")

    def _to_dto(self, message: "WorldMessage"):
        """WorldMessage -> rich frozen adapters.base DTO (edit-event parity).

        EDIT PARITY (audit round 3): live edit events carry the FULL
        :func:`adapters.model.convert_message` projection — the sim used to
        push a minimal 5-field DTO, so plugins testing against edits saw a
        poorer shape in sims than live. Now the SAME rich builder used for
        deliveries (``to_discord_message``) builds edit events too.
        """
        return to_discord_message(self, message)

    def delete_message(self, message_id: int) -> WorldMessage:
        """Remove a message and push a delete event (ids only, like live)."""
        for channel_id, messages in list(self.history.items()):
            for index, message in enumerate(messages):
                if message.id == message_id:
                    messages.pop(index)
                    for client in list(self.subscribers):
                        _sees = getattr(client, "can_see", None)
                        if (client.user_id != message.author_id
                                and client.on_message_delete is not None
                                and (_sees is None or _sees(channel_id))):
                            try:
                                client.on_message_delete(
                                    message_id, channel_id, message.guild_id, message)
                            except Exception:
                                LOG.exception(
                                    "world subscriber %r failed on delete %s",
                                    client, message_id)
                    return message
        raise KeyError(f"unknown message {message_id}")

    def _maybe_auto_respond(self, message: WorldMessage) -> None:
        """Scripted responder: an exact registered command triggers DM pages.

        Skips the responder's own messages and unknown authors (no echoing).
        """
        if self.responder_id is None:
            return
        text = message.content.strip()
        if text not in self.responses:
            return
        if message.author_id == self.responder_id or message.author_id not in self.users:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # delivered from a sync test context: nothing can await the reply
            return
        author, responder = message.author_id, self.responder_id
        task = loop.create_task(
            self._scripted_reply(author, responder, self.responses[text])
        )
        self._responder_tasks.add(task)
        task.add_done_callback(self._responder_tasks.discard)

    async def _scripted_reply(self, author: int, responder: int, pages: List[str]) -> None:
        """Deliver the scripted pages via DM to the requesting user."""
        await asyncio.sleep(self.reply_delay)
        channel_id = self.dm_channel_for(author, responder)
        for page in pages:
            await asyncio.sleep(self.reply_delay)
            if channel_id not in self.channels:  # DM channel vanished meanwhile
                return
            self.deliver(channel_id, responder, page, dm=True)

    def cancel_pending_replies(self) -> None:
        """Cancel scripted-responder tasks still in flight (run teardown)."""
        for task in list(self._responder_tasks):
            task.cancel()

    # ---------------------------------------------------------------- queries
    def messages_of(self, channel_id: int) -> List[str]:
        """Contents of one channel's history (empty once the channel is gone)."""
        return [m.content for m in self.history.get(channel_id, [])]

    def find_delivery(self, *, author_id: int, content: str) -> Optional[WorldMessage]:
        """First delivered message with exactly this author and content."""
        for message in self.journal:
            if message.author_id == author_id and message.content == content:
                return message
        return None

    async def wait_for_quiet(
        self, channel_id: int, quiet: float = 0.4, limit: float = 10.0
    ) -> None:
        """Return once no new messages appear in a channel for `quiet` seconds.

        AUDIT FIX (ported): previously this ran to the full `limit` regardless
        of silence (the quiet window was never checked) — every caller waited
        far longer than intended. Now it returns as soon as `quiet` seconds of
        silence accumulate, with `limit` as the hard cap. Deleted channels are
        silent immediately, so the wait degenerates to a single `quiet` window.
        """
        seen = len(self.history.get(channel_id, []))
        quiet_elapsed, total = 0.0, 0.0
        while total < limit:
            step = min(0.05, max(quiet - quiet_elapsed, 0.001))
            await asyncio.sleep(step)
            total += step
            current = len(self.history.get(channel_id, []))
            if current != seen:
                seen = current
                quiet_elapsed = 0.0
            else:
                quiet_elapsed += step
                if quiet_elapsed >= quiet:
                    return


__all__ = [
    "SimWorld",
    "WorldMessage",
    "to_discord_message",
    "SYNTHETIC_OWNER_ID",
    "SYNTHETIC_ALPHA_ID",
    "SYNTHETIC_BETA_ID",
    "SYNTHETIC_GUILD_CHANNEL",
    "SYNTHETIC_MUDAE_ID",
]
