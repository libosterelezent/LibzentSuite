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
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, List, Optional, Sequence

import aiosqlite

from core.errors import DatabaseError  # ONE class for the whole framework

LOG = logging.getLogger("slaughtermudae.database")

#: pragmas applied once per connection (see module docstring for rationale)
PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
)


#: how long close() waits for an in-flight transaction before closing anyway
CLOSE_TIMEOUT_S = 5.0


class _TaskGuard:
    """Re-entrant (per asyncio task) mutual exclusion for the ONE connection.

    SQLite on a single connection has ONE transaction state shared by every
    task, so a reader that runs while another task is mid-transaction sees
    that task's UNCOMMITTED rows (and a commit by anyone commits everyone).
    Every access therefore goes through this guard. The task that already
    owns it may re-enter (read its own writes, nest writes) without
    deadlocking; any other task waits its turn.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: Optional[asyncio.Task] = None

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[bool]:
        """Yield True for the outermost holder, False when re-entered."""
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            yield False
            return
        async with self._lock:
            self._owner = task
            try:
                yield True
            finally:
                self._owner = None

    async def acquire_bounded(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def release(self) -> None:
        self._lock.release()


def _touch_private(path: Path) -> None:
    """Create the DB file with owner-only permissions BEFORE sqlite opens it
    (no window where a fresh database is world-readable)."""
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
    except OSError:
        pass


class DatabaseConnection:
    """One aiosqlite connection + transaction context, explicitly owned."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path) if str(path) != ":memory:" else ":memory:"
        self._conn: Optional[aiosqlite.Connection] = None
        self._guard = _TaskGuard()  # one transaction state -> one guard

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        """Open the connection and apply pragmas (idempotent)."""
        if self._conn is not None:
            return
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            _touch_private(Path(self._path))
        conn = await aiosqlite.connect(str(self._path))
        try:
            conn.row_factory = aiosqlite.Row
            for pragma in PRAGMAS:
                await conn.execute(pragma)
            await conn.commit()
        except BaseException:
            # never leak the worker thread when setup fails (it would keep
            # the interpreter alive at exit)
            try:
                await conn.close()
            except Exception:
                pass
            raise
        self._conn = conn

    async def close(self, timeout: float = CLOSE_TIMEOUT_S) -> None:
        """Close the connection (idempotent; safe during shutdown).

        Waits (bounded) for an in-flight transaction so a graceful shutdown
        never cuts a write in half; after the timeout it closes anyway.
        """
        if self._conn is None:
            return
        locked = await self._guard.acquire_bounded(timeout)
        if not locked:
            LOG.warning("database close: transaction still running after "
                        "%.1fs; closing anyway", timeout)
        conn, self._conn = self._conn, None
        try:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    LOG.debug("database close failed", exc_info=True)
        finally:
            if locked:
                self._guard.release()

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError(
                "database is not connected (await Database.connect() first)")
        return self._conn

    # ------------------------------------------------------------- queries
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """One statement, committed (simple writes / pragmas / DDL).

        Inside the caller's own open transaction it joins it (no commit).
        """
        async with self._guard.hold() as outer:
            conn = self._require()
            await conn.execute(sql, tuple(params))
            if outer:
                await conn.commit()

    async def fetch_all(self, sql: str,
                        params: Sequence[Any] = ()) -> List[aiosqlite.Row]:
        async with self._guard.hold():
            conn = self._require()
            async with conn.execute(sql, tuple(params)) as cursor:
                return list(await cursor.fetchall())

    async def fetch_one(self, sql: str,
                        params: Sequence[Any] = ()) -> Optional[aiosqlite.Row]:
        async with self._guard.hold():
            conn = self._require()
            async with conn.execute(sql, tuple(params)) as cursor:
                return await cursor.fetchone()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        """Transaction: COMMIT on success, ROLLBACK on any exception.

        Writers (and readers) are serialized by the task guard. A task that
        already holds the transaction may nest ``write()``: the inner block
        joins the outer one and the OUTERMOST block commits/rolls back.
        """
        async with self._guard.hold() as outer:
            conn = self._require()
            if not outer:
                yield conn
                return
            try:
                yield conn
                await conn.commit()
            except BaseException:
                try:
                    await conn.rollback()
                except Exception:
                    LOG.debug("rollback failed", exc_info=True)
                raise

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        """Read access that never observes another task's open transaction."""
        async with self._guard.hold():
            yield self._require()


__all__ = ["DatabaseConnection", "DatabaseError", "PRAGMAS"]
