"""Per-account Discord cache: guilds, channels, DM channels — real-time first.

Ported wholesale from ``git show HEAD:core/discord_cache.py`` (audited through
3 review rounds) onto the rewrite spine, with two additions the rewrite needs:

  * :meth:`DiscordCache.dms` / :meth:`DiscordCache.dm_channel_for` /
    :meth:`DiscordCache.record_dm` — DM channels learned AFTER READY (via
    ``client.open_dm``) are recorded here so DM resolution stays cache-first,
  * :func:`resolve_dm_channel` — the shared cache-first DM resolution used by
    both ``AccountManager.send_hook`` and ``AccountPluginServices.open_dm``.

Principle (user spec): interact with Discord in real time, cache channels and
guilds where needed, RARELY use the fetch API.

Invalidation contract: when cached state goes bad (channel deleted, access
revoked) the cache raises a :class:`core.errors.TargetGone` (a
``FlowBlocked`` subclass) with a human reason — that account's plugin flow
stops while every other account keeps running.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..errors import AccountError, FlowBlocked, TargetGone

LOG = logging.getLogger("slaughtermudae.cache")


@dataclass
class ChannelInfo:
    """One cached channel (guild channel or DM) as seen at READY."""

    channel_id: int
    name: str = ""
    guild_id: Optional[int] = None
    dm: bool = False
    recipient: Optional[int] = None
    deleted: bool = False
    gone_since: Optional[float] = None


class DiscordCache:
    """In-memory view of one account's Discord world (populated at READY)."""

    def __init__(self, account: str, ttl: float = 0.0) -> None:
        """``account``: owning account name (used in every error message)."""
        self.account = account
        self.user_id: Optional[int] = None
        self.user_name: str = ""
        self.guilds: Dict[int, Dict[str, Any]] = {}
        self.channels: Dict[int, ChannelInfo] = {}
        #: listeners notified on invalidations (manager logs / dashboard)
        self._on_invalidate: List[Callable[[str, int, str], None]] = []
        self.ready = False

    # ------------------------------------------------------------ ingestion
    def ingest_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Replace/merge cache contents from a client ``cached_snapshot``.

        Zero fetches: only what the client already holds. DM merge rule
        (ported): a guild channel id can never be a DM channel, so when a
        snapshot DM collides with a known guild channel the guild view stays
        intact and only the recipient is recorded.
        """
        if not snapshot or not snapshot.get("available"):
            return
        user = snapshot.get("user") or {}
        self.user_id = user.get("id", self.user_id)
        self.user_name = user.get("name", self.user_name)
        for guild in snapshot.get("guilds", []):
            gid = guild.get("id")
            self.guilds[gid] = {"name": guild.get("name") or f"guild-{gid}", "channels": {}}
            for channel in guild.get("channels", []):
                cid = channel["id"]
                info = ChannelInfo(
                    channel_id=cid, name=channel.get("name") or f"channel-{cid}", guild_id=gid
                )
                self.channels[cid] = info
                self.guilds[gid]["channels"][cid] = info.name
        for dm in snapshot.get("dm_channels", []):
            cid = dm["id"]
            existing = self.channels.get(cid)
            recipient = dm.get("recipient")
            name = dm.get("name") or f"dm-{recipient or cid}"
            if existing is None:
                self.channels[cid] = ChannelInfo(
                    channel_id=cid, name=name, dm=True, recipient=recipient
                )
            else:  # guild view intact; just remember the recipient
                self.channels[cid].recipient = recipient
        self.ready = True

    # ------------------------------------------------------------ DM helper
    def dms(self) -> int:
        """Count of live (non-deleted) DM channels — details page metric."""
        return sum(1 for c in self.channels.values() if c.dm and not c.deleted)

    def dm_channel_for(self, user_id: int) -> Optional[int]:
        """Cached DM channel id for ``user_id`` (None when not cached)."""
        for info in self.channels.values():
            if info.dm and info.recipient == user_id and not info.deleted:
                return info.channel_id
        return None

    def record_dm(self, channel_id: int, recipient: Optional[int]) -> ChannelInfo:
        """Record a DM channel learned AFTER READY (via ``client.open_dm``).

        Creates the entry or refreshes the recipient on an existing one (the
        audited merge rule: a guild channel id can't become a DM). Re-opening
        a previously invalidated DM clears the deleted flag — the channel was
        just proven usable again.
        """
        info = self.channels.get(channel_id)
        if info is None:
            info = ChannelInfo(
                channel_id=channel_id,
                name=f"dm-{recipient or channel_id}",
                dm=True,
                recipient=recipient,
            )
            self.channels[channel_id] = info
        else:
            info.recipient = recipient
        info.deleted = False
        info.gone_since = None
        return info

    # -------------------------------------------------------------- queries
    def channel(self, channel_id: int) -> ChannelInfo:
        """Live ChannelInfo or :class:`TargetGone` when unknown/deleted."""
        info = self.channels.get(channel_id)
        if info is None or info.deleted:
            raise TargetGone(
                self.account,
                f"channel {channel_id} is not in the account's cache "
                "(deleted, or the account cannot see it)",
            )
        return info

    def describe(self, channel_id: int) -> str:
        """Human label: ``#name in guild`` / ``dm with <user>`` (never raises)."""
        try:
            info = self.channel(channel_id)
        except FlowBlocked:
            return f"channel-{channel_id}"
        if info.dm:
            return f"dm with {info.recipient}"
        guild = self.guilds.get(info.guild_id, {}).get("name", "?")
        return f"#{info.name} in {guild}"

    # ---------------------------------------------------------- invalidation
    def invalidate_channel(self, channel_id: int, reason: str) -> bool:
        """Mark a channel gone; notifies listeners; True when it was known."""
        info = self.channels.get(channel_id)
        if info is None:
            return False
        info.deleted = True
        info.gone_since = time.time()
        for callback in list(self._on_invalidate):
            try:
                callback(self.account, channel_id, reason)
            except Exception:
                LOG.exception("invalidation listener failed")
        LOG.warning("channel %s invalidated for %s: %s", channel_id, self.account, reason)
        return True

    def on_invalidate(self, callback: Callable[[str, int, str], None]) -> Callable[[], None]:
        """Subscribe an invalidation listener; returns an unsubscribable."""
        self._on_invalidate.append(callback)

        def _remove() -> None:
            if callback in self._on_invalidate:
                self._on_invalidate.remove(callback)

        return _remove

    # ------------------------------------------------------------ validation
    def validate_channel(self, channel_id: int) -> ChannelInfo:
        """Validate BEFORE acting: raises :class:`TargetGone` when gone.

        Central hook for the flow-abort contract: callers (send hook,
        plugins) let the FlowBlocked subclass propagate with the reason.
        """
        return self.channel(channel_id)

    def snapshot(self) -> Dict[str, Any]:
        """JSON-safe cache summary (manager status / dashboard)."""
        guild_list = [
            {"id": gid, "name": g["name"], "channels": len(g["channels"])}
            for gid, g in sorted(self.guilds.items())
        ]
        deleted = [cid for cid, c in self.channels.items() if c.deleted]
        return {
            "ready": self.ready,
            "user": {"id": self.user_id, "name": self.user_name},
            "guilds": guild_list,
            "channels_total": len(self.channels),
            "dms": self.dms(),
            "invalidated": deleted,
        }


async def resolve_dm_channel(
    cache: Optional[DiscordCache],
    client: Any,
    user_id: int,
    account: str = "",
) -> int:
    """Cache-first DM channel resolution (shared by send hook + services).

    1. look for a live cached DM channel with this recipient,
    2. on a miss, ``await client.open_dm(user_id)`` and record the result in
       the cache so subsequent sends never re-open it.

    Raises:
        AccountError: cache miss and no live client available.
    """
    if cache is not None:
        cached = cache.dm_channel_for(int(user_id))
        if cached is not None:
            return cached
    if client is None:
        raise AccountError(
            f"account {account!r} has no active client to open a DM with {user_id}"
        )
    channel_id = int(await client.open_dm(int(user_id)))
    if cache is not None:
        cache.record_dm(channel_id, int(user_id))
    return channel_id


__all__ = ["ChannelInfo", "DiscordCache", "resolve_dm_channel"]
