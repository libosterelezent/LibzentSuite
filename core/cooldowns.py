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
from typing import Any, Callable, Dict, List, Optional

LOG = logging.getLogger("slaughtermudae.cooldowns")


class CooldownService:
    """Persistent, account-scoped cooldowns with absolute expiry."""

    def __init__(self, repository: Optional[Any] = None,
                 time_fn: Callable[[], float] = None) -> None:
        self._repo = repository
        self._now = time_fn or time.time
        # (account, plugin, key) -> {"expires_at":..., ...} restored at boot
        self._active: Dict[tuple, Dict[str, Any]] = {}

    # ---------------------------------------------------------------- boot
    async def restore(self) -> Dict[str, int]:
        """Boot recovery: load live cooldowns, drop expired ones.

        Returns ``{"restored": n, "expired_cleared": m}`` for logging and
        the dashboard. Deterministic via the injected time seam in tests.
        """
        now = self._now()
        try:
            expired = await self._repo.clear_expired(now=now) \
                if self._repo is not None else 0
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
                self._active[(row["account"], row["plugin"],
                              row["cooldown_key"])] = row
                restored += 1
        if restored or expired:
            LOG.info("cooldowns restored=%d expired_cleared=%d",
                     restored, expired)
        return {"restored": restored, "expired_cleared": expired}

    # ------------------------------------------------------------------ api
    async def set(self, account: str, plugin: str, cooldown_key: str,
                  duration_seconds: float, *, source: str = "plugin",
                  reason: str = "",
                  metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
            raise ValueError(
                f"cooldown duration must be finite, got {duration_seconds!r}")
        now = self._now()
        expires_at = now + max(0.0, duration)
        row = {"account": account, "plugin": plugin,
               "cooldown_key": cooldown_key, "started_at": now,
               "expires_at": expires_at, "source": source,
               "reason": reason, "metadata": metadata or {}}
        if self._repo is not None:
            await self._repo.upsert(
                account=account, plugin=plugin, cooldown_key=cooldown_key,
                started_at=now, expires_at=expires_at, source=source,
                reason=reason, metadata=metadata)
        self._active[(account, plugin, cooldown_key)] = row
        LOG.info("cooldown set %s/%s/%s for %.0fs (source=%s)",
                 account, plugin, cooldown_key, duration_seconds, source)
        return dict(row)

    async def get(self, account: str, plugin: str, cooldown_key: str
                  ) -> Optional[Dict[str, Any]]:
        """The ACTIVE cooldown row, or None when absent/expired.

        Memory-first; consults the repository only on a miss so restarts
        and multi-process writes stay correct. Expired rows are lazily
        cleared.
        """
        key = (account, plugin, cooldown_key)
        row = self._active.get(key)
        now = self._now()
        if row is not None and row["expires_at"] > now:
            return dict(row)
        if row is not None:
            # expiry-scoped delete: another process may have EXTENDED the
            # row since our memory copy — only clear what is still expired
            if self._repo is not None:
                try:
                    await self._repo.clear_expired_for(
                        account, plugin, cooldown_key, self._now())
                except Exception:
                    LOG.debug("lazy cooldown clear failed", exc_info=True)
            else:
                await self.clear(account, plugin, cooldown_key)
            return None
        if self._repo is not None:
            fresh = await self._repo.get(account, plugin, cooldown_key,
                                         now=now)
            if fresh is not None:
                self._active[key] = fresh
                return dict(fresh)
        return None

    async def remaining(self, account: str, plugin: str, cooldown_key: str
                        ) -> float:
        """Seconds left on a cooldown (0.0 when none/expired)."""
        row = await self.get(account, plugin, cooldown_key)
        if row is None:
            return 0.0
        return max(0.0, row["expires_at"] - self._now())

    async def is_active(self, account: str, plugin: str, cooldown_key: str
                        ) -> bool:
        return await self.get(account, plugin, cooldown_key) is not None

    async def clear(self, account: str, plugin: str, cooldown_key: str
                    ) -> bool:
        """Remove one cooldown (memory + DB)."""
        existed = self._active.pop((account, plugin, cooldown_key), None) is not None
        if self._repo is not None:
            existed = await self._repo.clear(account, plugin, cooldown_key) or existed
        return existed

    async def active_for(self, account: str,
                         plugin: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every active cooldown of one account (optionally one plugin)."""
        now = self._now()
        rows = [dict(r) for r in self._active.values()
                if r["account"] == account and r["expires_at"] > now
                and (plugin is None or r["plugin"] == plugin)]
        if self._repo is not None:
            for row in await self._repo.active_for(account, plugin=plugin,
                                                   now=now):
                key = (account, row["plugin"], row["cooldown_key"])
                if key not in self._active:
                    rows.append(row)
        rows.sort(key=lambda r: r["expires_at"])
        return rows

    async def delete_account(self, account: str) -> int:
        """Cascade on account deletion (spec §32)."""
        for key in [k for k in self._active if k[0] == account]:
            self._active.pop(key, None)
        return await self._repo.delete_account(account) \
            if self._repo is not None else 0


__all__ = ["CooldownService"]
