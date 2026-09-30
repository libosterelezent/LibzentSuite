"""Per-account Discord cache: guilds, channels, DM channels — real-time first.

Principle (user spec): interact with Discord in real time, cache channels and
guilds where needed, RARELY use the fetch API. When a fetch IS needed it goes
through the account's single-flight REST gate (one call at a time, others
wait) so parallel fetches can never cause rate limits.

Invalidation contract: when cached state goes bad (channel deleted, access
revoked) the session raises a FlowBlocked subclass with a reason — that
account's plugin flow stops while every other account keeps running.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .errors import AccessDenied, CacheInvalid, FetchRequired, FlowBlocked, TargetGone

LOG = logging.getLogger("slaughtermudae.cache")


@dataclass
class ChannelInfo:
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
        self.account = account
        self.user_id: Optional[int] = None
        self.user_name: str = ""
        self.guilds: Dict[int, Dict[str, Any]] = {}
        self.channels: Dict[int, ChannelInfo] = {}
        #: listeners notified on invalidations (container logs / dashboard)
        self._on_invalidate: List[Callable[[str, int, str], None]] = []
        self.ready = False

    # ------------------------------------------------------------ ingestion
    def ingest_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Replace cache contents from a client cached_snapshot (zero fetches)."""
        if not snapshot.get("available"):
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
            else:  # a guild channel id can't be a DM; keep guild view intact
                self.channels[cid].recipient = recipient
        self.ready = True

    # -------------------------------------------------------------- queries
    def channel(self, channel_id: int) -> ChannelInfo:
        info = self.channels.get(channel_id)
        if info is None or info.deleted:
            raise TargetGone(
                self.account,
                f"channel {channel_id} is not in the account's cache "
                "(deleted, or the account cannot see it)",
            )
        return info

    def describe(self, channel_id: int) -> str:
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
        """Mark a channel gone; notifies listeners; returns True if known."""
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
        self._on_invalidate.append(callback)

        def _remove() -> None:
            if callback in self._on_invalidate:
                self._on_invalidate.remove(callback)

        return _remove

    # ------------------------------------------------------------ validation
    def validate_channel(self, channel_id: int) -> ChannelInfo:
        """Validate BEFORE acting: raises TargetGone when the target is gone.

        Central hook for the flow-abort contract: callers (sessions/plugins)
        let the FlowBlocked subclass propagate with the reason attached.
        """
        return self.channel(channel_id)

    def snapshot(self) -> Dict[str, Any]:
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
            "invalidated": deleted,
        }


class CacheManager:
    """Owns one DiscordCache per account + the invalidation wiring."""

    def __init__(self) -> None:
        self._caches: Dict[str, DiscordCache] = {}

    def cache_for(self, account: str) -> DiscordCache:
        cache = self._caches.get(account)
        if cache is None:
            cache = DiscordCache(account)
            self._caches[account] = cache
        return cache

    def drop(self, account: str) -> None:
        self._caches.pop(account, None)

    def snapshot(self) -> Dict[str, Any]:
        return {name: cache.snapshot() for name, cache in self._caches.items()}
