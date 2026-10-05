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

import asyncio
import logging
import time
from contextlib import nullcontext
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
        #: single-flight lock serializing DM resolution + invalidation for
        #: this account (shared by resolve_dm_channel/invalidate_dm_channel:
        #: a plugin-side open_dm and a lane dm send must dedupe to ONE
        #: client.open_dm, and an invalidation must never interleave INSIDE
        #: a resolution — its db delete could land before the resolver's
        #: write and resurrect the dead row)
        self._dm_lock = asyncio.Lock()
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
            else:  # guild view intact; refresh recipient + heal a stale
                existing.recipient = recipient  # deleted flag (the snapshot
                existing.deleted = False  # just proved the channel exists)
                existing.gone_since = None
        # FINAL-PHASE (F8 gap 4): sweep stale channel tombstones — a
        # deleted=True entry was retained forever (gone_since existed
        # precisely for this sweep); a fresh READY is the truth point.
        cutoff = time.time() - 86400.0  # gone >24h: not coming back quietly
        stale = [cid for cid, info in self.channels.items()
                 if info.deleted and info.gone_since is not None
                 and info.gone_since < cutoff]
        for cid in stale:
            del self.channels[cid]
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

    def learn_channel(self, channel_id: int, guild_id: Optional[int],
                      name: str = "") -> None:
        """Teach the cache a channel seen in INBOUND traffic.

        The READY snapshot is the only other teacher — without this, a
        channel created after READY (or a DM the other party opened) would
        fail validate_channel forever even though the account can clearly
        see (and could send to) it. Never downgrades an existing entry.
        """
        if channel_id in self.channels:
            return  # existing entry is authoritative (never downgraded)
        if guild_id is None:  # DM-shaped but unknown recipient
            self.channels[channel_id] = ChannelInfo(
                channel_id=channel_id, name=name or f"dm-{channel_id}", dm=True
            )
            return
        self.channels[channel_id] = ChannelInfo(
            channel_id=channel_id, name=name or f"channel-{channel_id}",
            guild_id=guild_id,
        )
        guild = self.guilds.get(guild_id)
        if guild is not None:
            guild["channels"][channel_id] = name or f"channel-{channel_id}"

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
    cache: Optional["DiscordCache"],
    client: Any,
    user_id: int,
    account: str = "",
    cache_repo: Any = None,
) -> int:
    """Cache-first DM channel resolution (shared by send hook + services).

    Resolution order (anti-flag: the REST API is the LAST resort):
    1. the account's live in-memory cache (from READY / prior sends),
    2. the account-scoped persistent cache (api_cache 'dm' row — survives
       restarts, so a DM costs ONE open for the account's lifetime),
    3. ``await client.open_dm(user_id)`` — then the result is recorded in
       BOTH caches so it is never fetched again.

    The whole resolution runs under the cache's per-account DM lock:
    concurrent callers (a plugin's open_dm racing a lane dm send) dedupe
    to exactly ONE open_dm. A persistent row whose channel the memory
    cache marked deleted THIS session is skipped — the row is stale (its
    invalidation's db delete failed), and trusting it would resurrect a
    proven-dead channel on every subsequent send.

    ``cache_repo`` is the (async) CacheRepository; every access is awaited
    and guarded — persistence must never break the send path.

    Raises:
        AccountError: full miss and no live client available.
    """
    user_id = int(user_id)
    async with (cache._dm_lock if cache is not None else nullcontext()):
        if cache is not None:
            cached = cache.dm_channel_for(user_id)
            if cached is not None:
                return cached
        if cache_repo is not None:
            try:
                stored = await cache_repo.get(account, "dm", str(user_id))
            except Exception:
                stored = None
            if isinstance(stored, int) and not isinstance(stored, bool):
                info = cache.channels.get(stored) if cache is not None else None
                if info is None or not info.deleted:
                    if cache is not None:  # promote the persistent row to memory
                        cache.record_dm(stored, user_id)
                    return stored
        if client is None:
            raise AccountError(
                f"account {account!r} has no active client to open a DM with {user_id}"
            )
        channel_id = int(await client.open_dm(user_id))
        if cache is not None:
            cache.record_dm(channel_id, user_id)
        if cache_repo is not None:
            try:
                await cache_repo.set(account, "dm", str(user_id), channel_id)
            except Exception:
                pass  # persistence must never break the send path
        return channel_id


async def invalidate_dm_channel(
    cache: Optional["DiscordCache"], cache_repo: Any, user_id: int, account: str = ""
) -> None:
    """Drop every cached DM mapping for a user (memory + database).

    Called when Discord reports the target gone — the next resolution
    re-opens the DM instead of hammering a dead channel id. Async: the
    persistent row is deleted through the (aiosqlite) cache repository.

    Runs under the cache's DM lock so it can never interleave inside a
    concurrent resolution (its db delete would land before the resolver's
    write and resurrect the dead row).
    """
    user_id = int(user_id)
    async with (cache._dm_lock if cache is not None else nullcontext()):
        if cache is not None:
            cached = cache.dm_channel_for(user_id)
            if cached is not None:
                cache.invalidate_channel(cached, f"dm with {user_id} gone")
        if cache_repo is not None:
            try:
                await cache_repo.delete(account, "dm", str(user_id))
            except Exception:
                LOG.warning(
                    "persistent dm row for account %s user %d survived "
                    "invalidation (db delete failed); it is ignored until "
                    "a fresh open succeeds", account, user_id,
                )


__all__ = ["ChannelInfo", "DiscordCache", "resolve_dm_channel", "invalidate_dm_channel"]
