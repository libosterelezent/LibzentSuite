"""CooldownService — generic persistent cooldowns with restart recovery.

Core owns persistence, lookup, expiration and cleanup; plugins own WHAT a
cooldown means and WHEN to create/clear it (spec §22). Expiry uses
ABSOLUTE epoch timestamps, so the restart scenario works exactly as
specified:

    45-minute cooldown set -> framework down 10 minutes -> restart
    -> ~35 minutes REMAIN (from expires_at), never a fresh 45.

All methods are async (aiosqlite repository underneath) and account-
scoped by the first argument — Account A can never observe Account B's
cooldowns (spec §25). A ``time_fn`` seam keeps restart tests
deterministic without wall-clock sleeps.
"""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

LOG = logging.getLogger("libzent.cooldowns")


class CooldownService:
    """Persistent, account-scoped cooldowns with absolute expiry."""

    def __init__(
        self, repository: Any | None = None, time_fn: Callable[[], float] | None = None
    ) -> None:
        self._repo = repository
        self._now = time_fn or time.time
        # (account, plugin, key) -> {"expires_at":..., ...} restored at boot
        self._active: dict[tuple, dict[str, Any]] = {}

    # ---------------------------------------------------------------- boot
    async def restore(self) -> dict[str, int]:
        """Boot recovery: load live cooldowns, drop expired ones.

        Returns ``{"restored": n, "expired_cleared": m}`` for logging and
        the dashboard. Deterministic via the injected time seam in tests.
        """
        now = self._now()
        try:
            expired = await self._repo.clear_expired(now=now) if self._repo is not None else 0
        except Exception:  # best-effort restore: a locked read must not
            LOG.exception("cooldown expiry sweep failed (continuing)")  # fail boot
            expired = 0
        restored = 0
        if self._repo is not None:
            try:
                active = await self._repo.active_all(now=now)
            except Exception:
                LOG.exception("cooldown restore failed (starting empty)")
                active = []
            for row in active:
                self._active[(row["account"], row["plugin"], row["cooldown_key"])] = row
                restored += 1
        if restored or expired:
            LOG.info("cooldowns restored=%d expired_cleared=%d", restored, expired)
        return {"restored": restored, "expired_cleared": expired}

    # ------------------------------------------------------------------ api
    async def set(
        self,
        account: str,
        plugin: str,
        cooldown_key: str,
        duration_seconds: float,
        *,
        source: str = "plugin",
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start (or replace) a cooldown lasting ``duration_seconds`` from NOW.

        Stores the ABSOLUTE ``expires_at`` — the persisted row survives
        restarts with the correct remaining time.
        """
        try:
            duration = float(duration_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"cooldown duration must be a number, got {duration_seconds!r}"
            ) from exc
        if not math.isfinite(duration):
            raise ValueError(f"cooldown duration must be finite, got {duration_seconds!r}")
        now = self._now()
        expires_at = now + max(0.0, duration)
        row = {
            "account": account,
            "plugin": plugin,
            "cooldown_key": cooldown_key,
            "started_at": now,
            "expires_at": expires_at,
            "source": source,
            "reason": reason,
            "metadata": metadata or {},
        }
        key = (account, plugin, cooldown_key)
        if self._repo is not None:
            await self._repo.upsert(
                account=account,
                plugin=plugin,
                cooldown_key=cooldown_key,
                started_at=now,
                expires_at=expires_at,
                source=source,
                reason=reason,
                metadata=metadata,
            )
            # AWAIT-RACE guard: a concurrent clear() completing while we
            # were parked on the upsert would be RESURRECTED in memory by
            # the mirror write below (the cleared cooldown lived on in
            # _active until the periodic resync). Re-read the row and
            # mirror only what the repository still holds — adopting the
            # fresh row also picks up same-key extensions by other
            # processes, matching resync()'s DB-wins rule.
            try:
                fresh = await self._repo.get(account, plugin, cooldown_key, now=self._now())
            except Exception:
                LOG.debug("cooldown post-upsert re-read failed", exc_info=True)
                fresh = row  # repo unreachable: keep the pre-race behavior
            if fresh is not None:
                # COPY, not alias: the repo owns its row objects (a test
                # double or in-process cache may hand back the stored dict)
                self._active[key] = dict(fresh)
            else:
                # the repository holds NO live row: cleared concurrently,
                # or this set() was zero/negative-duration (born expired,
                # filtered by the read). Either way memory must not keep a
                # previous, longer cooldown for the key.
                self._active.pop(key, None)
        else:
            self._active[key] = row
        # log the COERCED duration: the raw argument may be a numeric
        # string ("60"), which printf's %f rejects with a TypeError
        LOG.info(
            "cooldown set %s/%s/%s for %.0fs (source=%s)",
            account,
            plugin,
            cooldown_key,
            duration,
            source,
        )
        return dict(row)

    async def get(self, account: str, plugin: str, cooldown_key: str) -> dict[str, Any] | None:
        """The ACTIVE cooldown row, or None when absent/expired.

        Memory-first; consults the repository only on a miss — or ONCE
        more when memory says expired (another process may have EXTENDED
        the row since our copy; a still-live row is adopted, a truly
        expired one is lazily cleared and its memory copy dropped, so the
        round-trip is not repeated on every subsequent get).
        """
        key = (account, plugin, cooldown_key)
        row = self._active.get(key)
        now = self._now()
        if row is not None and row["expires_at"] > now:
            return dict(row)
        if row is not None:
            # memory says expired: drop the stale copy first, then re-check
            # the repository once — this both adopts rows EXTENDED by
            # another process and stops the expired row from shadowing
            # them forever
            self._active.pop(key, None)
            if self._repo is not None:
                # expiry-scoped delete: only clears what is still expired
                try:
                    await self._repo.clear_expired_for(account, plugin, cooldown_key, self._now())
                except Exception:
                    LOG.debug("lazy cooldown clear failed", exc_info=True)
                # one-shot re-check: adopts rows EXTENDED by another
                # process (FINAL-PHASE F8 gap 1 + round-3 — same fix from
                # both sides); a truly-expired row is gone for good now
                fresh = await self._repo.get(account, plugin, cooldown_key, now=self._now())
                if fresh is not None:
                    self._active[key] = fresh
                    return dict(fresh)
            return None
        if self._repo is not None:
            fresh = await self._repo.get(account, plugin, cooldown_key, now=now)
            if fresh is not None:
                self._active[key] = fresh
                return dict(fresh)
        return None

    async def remaining(self, account: str, plugin: str, cooldown_key: str) -> float:
        """Seconds left on a cooldown (0.0 when none/expired)."""
        row = await self.get(account, plugin, cooldown_key)
        if row is None:
            return 0.0
        return max(0.0, row["expires_at"] - self._now())

    async def is_active(self, account: str, plugin: str, cooldown_key: str) -> bool:
        return await self.get(account, plugin, cooldown_key) is not None

    async def clear(self, account: str, plugin: str, cooldown_key: str) -> bool:
        """Remove one cooldown (memory + DB).

        The repository delete runs FIRST and memory is popped only on
        success: popping memory first used to RESURRECT the cooldown on
        the next get() (the memory miss fell through to the repository,
        which still held the row the failed delete never removed).
        """
        existed = False
        if self._repo is not None:
            existed = await self._repo.clear(account, plugin, cooldown_key)
        popped = self._active.pop((account, plugin, cooldown_key), None)
        return existed or popped is not None

    async def active_for(self, account: str, plugin: str | None = None) -> list[dict[str, Any]]:
        """Every active cooldown of one account (optionally one plugin).

        AUTHORITATIVE view (repo merge) — used by the cooldowns panel and
        clears. The 1 Hz status poll uses :meth:`active_count_for`
        (memory-only) instead; external edits are absorbed by the
        periodic :meth:`resync`.
        """
        now = self._now()
        rows = [
            dict(r)
            for r in self._active.values()
            if r["account"] == account
            and r["expires_at"] > now
            and (plugin is None or r["plugin"] == plugin)
        ]
        if self._repo is not None:
            by_key = {(r["account"], r["plugin"], r["cooldown_key"]): i for i, r in enumerate(rows)}
            for row in await self._repo.active_for(account, plugin=plugin, now=now):
                key = (account, row["plugin"], row["cooldown_key"])
                at = by_key.get(key)
                if at is None:
                    rows.append(row)
                elif row["expires_at"] > rows[at]["expires_at"]:
                    rows[at] = row  # extended by another process: DB wins
        rows.sort(key=lambda r: r["expires_at"])
        return rows

    def active_count_for(self, account: str) -> int:
        """Memory-only active count (FINAL-PHASE F3: status-poll path).

        The mirror is boot-restored and written on every set/clear, so
        in-process truth is immediate; external edits are picked up by
        the periodic resync instead of a per-second DB scan.
        """
        return self.active_counts_all().get(account, 0)

    def active_counts_all(self) -> dict:
        """One O(C) pass computing EVERY account's active count.

        status() needs the count for all accounts per build; calling
        active_count_for per account was O(N accounts x C rows) — a
        500-account fleet with 10k rows stalled the loop ~300ms/s.
        """
        now = self._now()
        counts: dict = {}
        for r in self._active.values():
            if r["expires_at"] > now:
                counts[r["account"]] = counts.get(r["account"], 0) + 1
        return counts

    async def resync(self) -> int:
        """Rebuild the memory mirror from the repository (DB wins per key).

        FINAL-PHASE (F3): bounds ALL external-edit divergence (a second
        process adding, extending, or clearing rows) to one resync
        period — the old per-second active_for() merge only ever caught
        NEW keys, never extensions/clears of mirrored keys. Also purges
        expired tombstones (F8 gap 1: rows were retained forever).
        """
        if self._repo is None:
            return 0
        rows = await self._repo.active_all(now=self._now())
        fresh = {(r["account"], r["plugin"], r["cooldown_key"]): r for r in rows}
        self._active = fresh
        return len(fresh)

    async def delete_account(self, account: str) -> int:
        """Cascade on account deletion (spec §32)."""
        for key in [k for k in self._active if k[0] == account]:
            self._active.pop(key, None)
        return await self._repo.delete_account(account) if self._repo is not None else 0


__all__ = ["CooldownService"]
