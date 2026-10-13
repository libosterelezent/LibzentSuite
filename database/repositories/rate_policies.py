"""RatePolicyRepository — the 4-level rate policy store (spec §5).

Rows live in ``rate_limit_policies`` keyed by (level, owner, action).
Owner is stored as TEXT ('' for global — the column is NOT NULL) and mapped
to/from RatePolicyRecord.owner (None for global) at the boundary.

Validation split (deliberate): this repository enforces *data integrity*
(finite numbers, 0 <= jitter_min <= jitter_max, valid level/owner shape);
the 1.5s safety floor is NOT checked here — that is
core.rate_limit_manager's job (UnsafePolicyError), because the floor is a
policy decision, not a storage invariant.
"""
from __future__ import annotations

import math
import sqlite3
from typing import List, Optional

from core.errors import DatabaseError

from database.models import RatePolicyRecord

from .base import BaseRepository

__all__ = ["RatePolicyRepository"]


class RatePolicyRepository(BaseRepository):
    """get/set/delete/list for rate policies across the four levels."""

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _owner_key(level: str, owner: Optional[str]) -> str:
        """Normalize an owner to its storage form ('' for global).

        Raises DatabaseError for a valid level with a missing owner (that
        combination can never address a row).
        """
        if level == "global":
            return ""
        if owner is None or owner == "":
            raise DatabaseError(f"policy level {level!r} requires an owner")
        return owner

    def _validated(
        self,
        level: str,
        owner: Optional[str],
        action: str,
        interval: float,
        jitter_min: float,
        jitter_max: float,
    ) -> RatePolicyRecord:
        """Build a RatePolicyRecord after integrity checks (write path).

        Raises DatabaseError for a bad level/owner shape, non-finite or
        mis-ordered numbers. The record itself enforces level/owner shape;
        number checks live here because the dataclass does not do them.
        """
        try:
            interval_f = float(interval)
            jmin = float(jitter_min)
            jmax = float(jitter_max)
        except (TypeError, ValueError) as exc:
            raise DatabaseError(f"rate policy numbers are invalid: {exc}") from exc
        for label, value in (("interval", interval_f), ("jitter_min", jmin), ("jitter_max", jmax)):
            if not math.isfinite(value):
                raise DatabaseError(f"rate policy {label} must be finite, got {value!r}")
        if jmin < 0 or jmax < 0:
            raise DatabaseError(f"rate policy jitter values must be >= 0 (got {jmin}, {jmax})")
        if jmin > jmax:
            raise DatabaseError(
                f"rate policy jitter_min ({jmin}) must be <= jitter_max ({jmax})"
            )
        if not isinstance(action, str) or not action:
            raise DatabaseError("rate policy action must be a non-empty string")
        try:
            return RatePolicyRecord(
                level=level,
                owner=(owner or None),
                action=action,
                interval=interval_f,
                jitter_min=jmin,
                jitter_max=jmax,
            )
        except ValueError as exc:
            raise DatabaseError(f"invalid rate policy: {exc}") from exc

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> RatePolicyRecord:
        """Map one row to a record (owner '' -> None for global)."""
        return RatePolicyRecord(
            level=row["level"],
            owner=(row["owner"] or None),
            action=row["action"],
            interval=float(row["interval"]),
            jitter_min=float(row["jitter_min"]),
            jitter_max=float(row["jitter_max"]),
        )

    # --------------------------------------------------------------- write
    async def set(
        self,
        level: str,
        owner: Optional[str],
        action: str,
        interval: float,
        jitter_min: float,
        jitter_max: float,
    ) -> None:
        """Upsert one policy row (level+owner+action is the key).

        The global level normalizes any owner to '' (there is exactly one
        global scope). Raises DatabaseError for invalid level/owner/numbers
        (see module docstring for what is — deliberately — NOT validated).
        """
        record = self._validated(level, owner, action, interval, jitter_min, jitter_max)
        owner_key = self._owner_key(record.level, record.owner)
        async with self._tx(f"set policy {record.level}/{owner_key}/{record.action}") as conn:
            await conn.execute(
                """INSERT INTO rate_limit_policies
                       (level, owner, action, interval, jitter_min, jitter_max)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(level, owner, action) DO UPDATE SET
                     interval = excluded.interval,
                     jitter_min = excluded.jitter_min,
                     jitter_max = excluded.jitter_max""",
                (record.level, owner_key, record.action,
                 record.interval, record.jitter_min, record.jitter_max),
            )

    async def delete(self, level: str, owner: Optional[str], action: str) -> bool:
        """Remove one policy; True when a row was deleted.

        Lenient by design (writes strict, reads/deletes forgiving): an
        unknown level or owner simply matches no rows and returns False —
        clear_policy surfaces that as "nothing to clear".
        """
        owner_key = "" if level == "global" else (owner or "")
        async with self._tx(f"delete policy {level}/{owner_key}/{action}") as conn:
            async with conn.execute(
                "DELETE FROM rate_limit_policies WHERE level = ? AND owner = ? AND action = ?",
                (level, owner_key, action),
            ) as cur:
                return cur.rowcount > 0

    async def delete_account(self, account: str) -> int:
        """Delete every account-scoped policy row for one account name.

        Account-removal cascade (isolation audit K15): covers
        level='account' (owner = name) and level='account_plugin'
        (owner = 'name/plugin'). Orphaned owner rows would otherwise be
        silently re-inherited by a re-created same-named account. Returns
        the number of rows removed.
        """
        removed = 0
        pattern = (
            account.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "/%"
        )
        async with self._tx(f"delete policies for account {account!r}") as conn:
            async with conn.execute(
                "DELETE FROM rate_limit_policies WHERE level = ? AND owner = ?",
                ("account", account),
            ) as cur:
                removed += cur.rowcount
            async with conn.execute(
                "DELETE FROM rate_limit_policies WHERE level = ? "
                "AND owner LIKE ? ESCAPE '\\'",
                ("account_plugin", pattern),
            ) as cur:
                removed += cur.rowcount
        return removed

    # ---------------------------------------------------------------- read
    async def get(self, level: str, owner: Optional[str], action: str) -> Optional[RatePolicyRecord]:
        """One policy by exact scope (None when nothing stored there).

        Owner forms are normalized on lookup: None and '' both address the
        global row, so callers never have to remember which spelling stuck.
        """
        owner_key = "" if level == "global" else (owner or "")
        row = await self._fetch_one(
            "SELECT * FROM rate_limit_policies WHERE level = ? AND owner = ? AND action = ?",
            (level, owner_key, action),
        )
        return None if row is None else self._record_from_row(row)

    async def list(self) -> List[RatePolicyRecord]:
        """Every stored policy (level, owner, action order).

        A row whose level/owner no longer constructs a valid record (corrupt
        row, future rename) is skipped with a warning instead of crashing.
        """
        records: List[RatePolicyRecord] = []
        for row in await self._fetch_all("SELECT * FROM rate_limit_policies ORDER BY level, owner, action"):
            try:
                records.append(self._record_from_row(row))
            except ValueError as exc:
                from .base import LOG

                LOG.warning("skipping corrupt rate policy row %r: %s", tuple(row), exc)
        return records
