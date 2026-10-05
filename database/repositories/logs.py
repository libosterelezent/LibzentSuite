"""AuditRepository + LogRepository — the two bounded append-only trails.

* ``audit_logs`` — the interaction trail (spec §6): every inbound/outbound
  message with wait/jitter numbers, pruned to ``audit_keep`` newest rows on
  every insert (ported SQL from the audited old implementation).
* ``logs`` — the structured log sink LogService attaches to, pruned to
  ``log_keep`` newest rows the same way.

Both return rows as plain dicts with float timestamps; ``recent`` fetches
newest-first then reverses, so callers always see OLDEST-FIRST (tail)
ordering — matching the old audit code and LogService.tail.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from core.errors import DatabaseError

from .base import BaseRepository

__all__ = ["AuditRepository", "LogRepository"]


_AUDIT_SQL = """INSERT INTO audit_logs
       (ts, account, direction, action, channel_id, content,
        wait_ms, jitter_ms, note)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""

_LOG_SQL = """INSERT INTO logs (ts, severity, source, account, plugin, task_id, event, message)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""


def _audit_row(entry: Dict[str, Any]) -> tuple:
    if not isinstance(entry, dict):
        raise DatabaseError(f"audit entry must be a dict, got {type(entry).__name__}")
    return (
        entry.get("ts", time.time()),
        entry.get("account", "?"),
        entry.get("direction", "out"),
        entry.get("action", "?"),
        entry.get("channel_id"),
        str(entry.get("content") or "")[:500],
        entry.get("wait_ms", 0.0),
        entry.get("jitter_ms", 0.0),
        entry.get("note"),
    )


def _log_row(entry: Dict[str, Any]) -> tuple:
    if not isinstance(entry, dict):
        raise DatabaseError(f"log entry must be a dict, got {type(entry).__name__}")
    return (
        entry.get("ts", time.time()),
        str(entry.get("severity") or "INFO").upper(),
        str(entry.get("source") or "?"),
        entry.get("account"),
        entry.get("plugin"),
        entry.get("task_id"),
        entry.get("event"),
        str(entry.get("message") or ""),
    )


class AuditRepository(BaseRepository):
    """Interaction audit trail (bounded, newest-kept pruning)."""

    def __init__(self, database, keep: int = 10_000) -> None:
        """``keep`` is the pruning bound applied on every add."""
        super().__init__(database)
        self._keep = max(0, int(keep))

    async def add(self, entry: Dict[str, Any]) -> None:
        """Append one interaction record, then prune to the newest ``keep``.

        Missing fields get audited-old defaults ('?' markers) so a malformed
        caller still leaves a traceable row; content is truncated to 500
        chars (old behavior — keeps the DB bounded against huge pastes).

        Raises DatabaseError when entry is not a dict or the write fails.
        """
        row = _audit_row(entry)
        async with self._tx("append audit entry") as conn:
            await conn.execute(_AUDIT_SQL, row)
            await self._prune_newest(conn, "audit_logs", self._keep)

    async def add_many(self, entries: List[Dict[str, Any]]) -> int:
        """Append a batch in ONE transaction with ONE prune (the queued
        drain worker's fast path); all-or-nothing. Returns rows written."""
        rows = [_audit_row(e) for e in entries]
        if not rows:
            return 0
        async with self._tx("append audit batch") as conn:
            await conn.executemany(_AUDIT_SQL, rows)
            await self._prune_newest(conn, "audit_logs", self._keep)
        return len(rows)

    async def recent(self, limit: int = 100, account: Optional[str] = None) -> List[Dict[str, Any]]:
        """The newest ``limit`` audit rows, returned oldest-first.

        ``account`` filters to one account's interactions. The DESC-fetch +
        reverse is the audited old pattern (id order == insert order, even
        when timestamps collide).
        """
        limit = max(0, int(limit))
        if account:
            rows = await self._fetch_all(
                "SELECT * FROM audit_logs WHERE account = ? ORDER BY id DESC LIMIT ?",
                (account, limit),
                what="read audit trail",
            )
        else:
            rows = await self._fetch_all(
                "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?",
                (limit,),
                what="read audit trail",
            )
        return [dict(row) for row in reversed(rows)]


class LogRepository(BaseRepository):
    """Structured log persistence (bounded, newest-kept pruning)."""

    def __init__(self, database, keep: int = 10_000) -> None:
        """``keep`` is the pruning bound applied on every add."""
        super().__init__(database)
        self._keep = max(0, int(keep))

    async def add(self, entry: Dict[str, Any]) -> None:
        """Append one structured log entry, then prune to the newest ``keep``.

        Accepts the LogEntry.as_dict() shape; missing fields default safely.
        Severity is upper-cased so dashboard filters match regardless of who
        wrote the row. Raises DatabaseError for a non-dict entry.
        """
        row = _log_row(entry)
        async with self._tx("append log entry") as conn:
            await conn.execute(_LOG_SQL, row)
            await self._prune_newest(conn, "logs", self._keep)

    async def add_many(self, entries: List[Dict[str, Any]]) -> int:
        """Append a batch in ONE transaction with ONE prune (all-or-nothing)."""
        rows = [_log_row(e) for e in entries]
        if not rows:
            return 0
        async with self._tx("append log batch") as conn:
            await conn.executemany(_LOG_SQL, rows)
            await self._prune_newest(conn, "logs", self._keep)
        return len(rows)

    async def recent(
        self,
        limit: int = 100,
        *,
        account: Optional[str] = None,
        severity: Optional[str] = None,
        source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """The newest ``limit`` log rows (oldest-first), optionally filtered.

        Filters AND together: account=, severity= (case-insensitive — stored
        upper-case), source=. Mirrors LogService.tail's contract. Each
        filter is an exact-None check: an explicit empty string is a
        filter matching nothing-real, not a silently-dropped filter.
        """
        limit = max(0, int(limit))
        sql = "SELECT * FROM logs WHERE 1 = 1"
        params: List[Any] = []
        if account is not None:
            sql += " AND account = ?"
            params.append(account)
        if severity is not None:
            sql += " AND severity = ?"
            params.append(str(severity).upper())
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = await self._fetch_all(sql, params, what="read logs")
        return [dict(row) for row in reversed(rows)]
