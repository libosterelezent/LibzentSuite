"""Async database connection layer (aiosqlite) — single long-lived connection.

Why aiosqlite here (spec §4): the framework is asyncio-native; the old
synchronous sqlite3 calls ran INLINE on the event loop (logging, audit,
manager mutations) — ~0.2-0.4ms stalls per call, and under bursts they
compound. aiosqlite moves every SQLite operation onto ONE dedicated worker
thread behind an async queue: awaiting a query never blocks the loop.

Honest limits (documented, per spec): aiosqlite does NOT make SQLite
concurrently writable — the single connection serializes writers exactly
as before; it removes the EVENT-LOOP stall, not the single-writer rule.

Connection ownership (spec §6): exactly ONE connection per Database,
created by :meth:`DatabaseConnection.connect`, owned by the Database
service, closed by :meth:`close`. No per-query/per-log connections exist
anywhere in the codebase.

SQLite configuration (spec §8) — each pragma justified:
* ``journal_mode=WAL``        — readers never block the writer; the audit/
                                logs read paths (dashboard) stay responsive.
* ``busy_timeout=5000``       — the ONLY second connection (rare offline
                                inspection) waits instead of failing BUSY.
* ``foreign_keys=ON``         — new tables (credentials/cooldowns) declare
                                FKs; legacy tables keep their proven manual
                                cascades (reworking them buys nothing).
* ``synchronous=NORMAL``      — WAL default; durable-enough for derived
                                state (cooldowns/state) with far fewer
                                fsyncs than FULL. Secrets are tiny rows.

Transactions: ``async with conn.write()`` — COMMIT on success, ROLLBACK on
exception. Repos never manage cursors/commits themselves.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence

import aiosqlite

LOG = logging.getLogger("slaughtermudae.database")

#: pragmas applied once per connection (see module docstring for rationale)
PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
)


class DatabaseError(RuntimeError):
    """Persistence-layer failure (kept importable from database.db)."""


class DatabaseConnection:
    """One aiosqlite connection + transaction context, explicitly owned."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path) if str(path) != ":memory:" else ":memory:"
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()  # serializes writers (SQLite rule)

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        """Open the connection and apply pragmas (idempotent)."""
        if self._conn is not None:
            return
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = aiosqlite.Row
        for pragma in PRAGMAS:
            await self._conn.execute(pragma)
        await self._conn.commit()

    async def close(self) -> None:
        """Close the connection (idempotent; safe during shutdown)."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError(
                "database is not connected (await Database.connect() first)")
        return self._conn

    # ------------------------------------------------------------- queries
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """One statement, committed (simple writes / pragmas / DDL)."""
        conn = self._require()
        async with self._lock:
            await conn.execute(sql, tuple(params))
            await conn.commit()

    async def fetch_all(self, sql: str,
                        params: Sequence[Any] = ()) -> List[aiosqlite.Row]:
        conn = self._require()
        async with conn.execute(sql, tuple(params)) as cursor:
            return list(await cursor.fetchall())

    async def fetch_one(self, sql: str,
                        params: Sequence[Any] = ()) -> Optional[aiosqlite.Row]:
        conn = self._require()
        async with conn.execute(sql, tuple(params)) as cursor:
            return await cursor.fetchone()

    @asynccontextmanager
    async def write(self) -> Iterator[aiosqlite.Connection]:
        """Transaction: COMMIT on success, ROLLBACK on any exception.

        Writers are serialized by an asyncio lock — SQLite allows one
        writer; the lock turns SQLITE_BUSY races into an ordered queue.
        """
        conn = self._require()
        async with self._lock:
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    @asynccontextmanager
    async def read(self) -> Iterator[aiosqlite.Connection]:
        """Read access without the writer lock (WAL readers don't block)."""
        yield self._require()


__all__ = ["DatabaseConnection", "DatabaseError", "PRAGMAS"]
