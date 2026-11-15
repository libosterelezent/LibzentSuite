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

Transactions: ``async with conn.write()`` — explicit ``BEGIN IMMEDIATE``
on entry, COMMIT on success, ROLLBACK on exception. Beginning eagerly
(write lock up front) instead of SQLite's deferred default removes the
cross-connection upgrade race: a deferred writer that first reads holds
a read snapshot, and its later write returns SQLITE_BUSY immediately
(busy_timeout cannot retry a stale snapshot) the moment another
connection commits in between — exactly the "sim runner + live" pattern.
Repos never manage cursors/commits themselves.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any

import aiosqlite

from core.errors import DatabaseError  # ONE class for the whole framework

from .telemetry import TELEMETRY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

LOG = logging.getLogger("libzent.database")

#: pragmas applied once per connection (see module docstring for rationale)
PRAGMAS = (
    # kv prefix matching is account-scoped: SQLite's LIKE is ASCII
    # case-INSENSITIVE by default, which made delete_prefix("...:alfa:")
    # also wipe "...:Alfa:..." (accounts names are case-sensitive-unique)
    ("case_sensitive_like", "ON"),
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
)


#: how long close() waits for an in-flight transaction before closing anyway
CLOSE_TIMEOUT_S = 5.0

#: how long any task may wait for the single connection before a clear
#: DatabaseError replaces the silent alternative: the classic deadlock is a
#: write() body that awaits OTHER database work (gather) — the child waits
#: for the lock the parent holds, forever. Generous enough that legitimate
#: writer queues (write storms, big batches) never trip it.
LOCK_WAIT_TIMEOUT_S = 30.0


class _TaskGuard:
    """Re-entrant (per asyncio task) mutual exclusion for the ONE connection.

    SQLite on a single connection has ONE transaction state shared by every
    task, so a reader that runs while another task is mid-transaction sees
    that task's UNCOMMITTED rows (and a commit by anyone commits everyone).
    Every access therefore goes through this guard. The task that already
    owns it may re-enter (read its own writes, nest writes) without
    deadlocking; any other task waits its turn — but only up to
    LOCK_WAIT_TIMEOUT_S, because a parent that awaits gathered child DB
    work inside its own write() block deadlocks the child against itself.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _check_loop(self) -> None:
        """One connection, one event loop — forever.

        aiosqlite futures bind to whichever loop submits them, so a second
        loop would interleave work on the SAME sqlite session while the
        asyncio.Lock only serializes within its own loop. The fast path of
        Lock.acquire would silently allow it; fail loud instead.
        """
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif loop is not self._loop:
            raise DatabaseError(
                "database connection used from multiple event loops; keep one "
                "Database per loop (use asyncio.run_coroutine_threadsafe to "
                "submit work to the owning loop)"
            )

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[bool]:
        """Yield True for the outermost holder, False when re-entered."""
        self._check_loop()
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            yield False
            return
        # G1: GUARD_WAIT — the producer the telemetry module docstring
        # promises (caller-side cross-task queuing). Only the bounded
        # acquire is timed — the re-enter fast path above never waits —
        # and ok=False marks the timeout so failed waits land in the
        # error counters like every other failure kind.
        if TELEMETRY.enabled:
            _t0 = perf_counter()
            acquired = await self.acquire_bounded(LOCK_WAIT_TIMEOUT_S)
            TELEMETRY.record("guard_wait", perf_counter() - _t0, ok=acquired)
        else:
            acquired = await self.acquire_bounded(LOCK_WAIT_TIMEOUT_S)
        if not acquired:
            raise DatabaseError(
                f"database lock not acquired within {LOCK_WAIT_TIMEOUT_S}s: "
                f"task {task!r} waited for the connection held by "
                f"{self._owner!r}; awaiting other database work (e.g. gather) "
                f"inside a write()/read() block deadlocks the single "
                f"connection"
            )
        self._owner = task
        try:
            yield True
        finally:
            self._owner = None
            self._lock.release()

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
        # O_EXCL: the create must atomically FAIL when the name already
        # exists — a plain O_CREAT would happily open (and chmod-probe)
        # through a symlink an attacker parked at the database path.
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError as exc:
        # a normal restart finds the previous boot's REGULAR file — fine.
        # Anything else (a symlink squat would aim the database at another
        # file) is refused loud, never followed: lstat does not resolve it.
        try:
            mode = os.lstat(str(path)).st_mode
        except OSError:
            return  # vanished mid-check: sqlite will surface the real state
        if not stat.S_ISREG(mode):
            raise DatabaseError(
                f"refusing database path {str(path)!r}: existing entry "
                f"is not a regular file (symlink?)"
            ) from exc
    except OSError:
        pass


class DatabaseConnection:
    """One aiosqlite connection + transaction context, explicitly owned."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path) if str(path) != ":memory:" else ":memory:"
        self._conn: aiosqlite.Connection | None = None
        self._guard = _TaskGuard()  # one transaction state -> one guard
        self._connect_lock = asyncio.Lock()  # concurrent connect()s leak workers

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        """Open the connection and apply pragmas (idempotent, race-free).

        Two concurrent connect() calls would each open an aiosqlite
        connection; the loser's worker thread (non-daemon) would keep the
        interpreter alive forever — hence the lock. The loop check runs
        BEFORE anything opens: a connect() from a second event loop must
        fail loud HERE — otherwise it would open a fresh aiosqlite
        worker, first USE would raise multi-loop, and the never-closed
        non-daemon worker would hang interpreter shutdown.
        """
        self._guard._check_loop()
        # both checks read through the `connected` property on purpose: a
        # direct `self._conn is not None` check narrows the attribute for
        # mypy across the await, hiding that the lock winner may have
        # connected while this task waited (double-checked locking).
        if self.connected:
            return
        async with self._connect_lock:
            if self.connected:
                # reachable only when another task's connect() won the
                # lock while this one waited; mypy's attribute narrowing
                # cannot see cross-task mutation, hence the local ignore
                return  # type: ignore[unreachable]
            if self._path != ":memory:":
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
                _touch_private(Path(self._path))
            # isolation_level=None: full manual transaction control. write()
            # issues BEGIN IMMEDIATE explicitly (see module docstring);
            # single statements run in autocommit exactly as before, where
            # execute() committed each one anyway.
            # FINAL-PHASE (F7 follow-up): the umask is tightened AROUND the
            # open + pragma sequence so the -wal/-shm sidecars SQLite
            # creates here (journal_mode=WAL) are BORN 0600 — the later
            # chmod in Database.connect() still tightens pre-existing
            # looser files, but until now a fresh sidecar sat world-
            # readable for that whole first window. Saved and restored,
            # never just set: the process keeps its umask. Concurrent
            # connects on OTHER connections can interleave restores; the
            # only reachable end state is {original, 0o077} — never a
            # umask looser than either caller wanted.
            _umask = os.umask(0o077)
            try:
                conn = await aiosqlite.connect(str(self._path), isolation_level=None)
                try:
                    conn.row_factory = aiosqlite.Row
                    for pragma in PRAGMAS:
                        # entries are either full "PRAGMA x=y" strings or (name, value)
                        statement = (
                            pragma
                            if isinstance(pragma, str)
                            else (f"PRAGMA {pragma[0]}={pragma[1]}")
                        )
                        await conn.execute(statement)
                    await conn.commit()
                except BaseException:
                    # never leak the worker thread when setup fails (it would keep
                    # the interpreter alive at exit)
                    with suppress(Exception):
                        await conn.close()
                    raise
            finally:
                os.umask(_umask)
            self._conn = conn
            TELEMETRY.set_worker_thread(conn)  # G1: worker TID for thread CPU

    async def close(self, timeout: float = CLOSE_TIMEOUT_S) -> None:
        """Close the connection (idempotent; safe during shutdown).

        Waits (bounded) for an in-flight transaction so a graceful shutdown
        never cuts a write in half; after the timeout it closes anyway.
        A cleanly closed connection may be reopened on ANOTHER event loop:
        the guard and connect lock are loop-bound once awaited, so they
        are reborn here (the guard is REPLACED, never mutated — holders
        and waiters of the old object stay balanced on its old lock).
        """
        if self._conn is None:
            return
        guard = self._guard
        locked = await guard.acquire_bounded(timeout)
        if not locked:
            LOG.warning(
                "database close: transaction still running after %.1fs; closing anyway", timeout
            )
        conn, self._conn = self._conn, None
        try:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    LOG.debug("database close failed", exc_info=True)
        finally:
            if locked:
                guard.release()
                if self._guard is guard:  # a concurrent close already
                    self._guard = _TaskGuard()  # reborn them: don't orphan
                    self._connect_lock = asyncio.Lock()  # in-flight guards

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError("database is not connected (await Database.connect() first)")
        return self._conn

    # ------------------------------------------------------------- queries
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """One statement, committed (simple writes / pragmas / DDL).

        Inside the caller's own open transaction it joins it (no commit).
        The statement's cursor is always closed: an unreset SELECT would
        pin a read snapshot on this connection and make the next
        write() BEGIN fail SQLITE_BUSY (see write()).
        """
        async with self._guard.hold() as outer:
            conn = self._require()
            if TELEMETRY.enabled:
                _t0 = perf_counter()
                _ok = True
                try:
                    async with conn.execute(sql, tuple(params)):
                        pass
                except BaseException:
                    _ok = False
                    raise
                finally:
                    TELEMETRY.record("write_stmt", perf_counter() - _t0, ok=_ok)
                if outer:
                    await conn.commit()
                return
            async with conn.execute(sql, tuple(params)):
                pass
            if outer:
                await conn.commit()

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._guard.hold():
            conn = self._require()
            if TELEMETRY.enabled:
                # ok=False on failure — the execute()/write() pattern: read
                # errors must land in the error counters too, not vanish
                # into a finally that always records ok
                _t0 = perf_counter()
                _ok = True
                try:
                    async with conn.execute(sql, tuple(params)) as cursor:
                        return list(await cursor.fetchall())
                except BaseException:
                    _ok = False
                    raise
                finally:
                    TELEMETRY.record("read", perf_counter() - _t0, ok=_ok)
            async with conn.execute(sql, tuple(params)) as cursor:
                return list(await cursor.fetchall())

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self._guard.hold():
            conn = self._require()
            if TELEMETRY.enabled:
                _t0 = perf_counter()
                _ok = True
                try:
                    async with conn.execute(sql, tuple(params)) as cursor:
                        return await cursor.fetchone()
                except BaseException:
                    _ok = False
                    raise
                finally:
                    TELEMETRY.record("read", perf_counter() - _t0, ok=_ok)
            async with conn.execute(sql, tuple(params)) as cursor:
                return await cursor.fetchone()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        """Transaction: BEGIN IMMEDIATE, COMMIT on success, ROLLBACK on error.

        Writers (and readers) are serialized by the task guard. The write
        lock is taken EAGERLY (not deferred) so a second connection to the
        same file never sees a stale-snapshot upgrade (immediate SQLITE_BUSY
        that busy_timeout cannot resolve). A task that already holds the
        transaction may nest ``write()``: the inner block joins the outer
        one and the OUTERMOST block commits/rolls back.

        Cursor rule for bodies that SELECT directly on the yielded
        connection: consume the rows AND let the cursor close (``async with
        conn.execute(...)``). An unreset SELECT pins this connection's read
        snapshot; the next BEGIN IMMEDIATE here then fails BUSY immediately
        once another connection commits — busy_timeout cannot retry a
        stale snapshot.
        """
        async with self._guard.hold() as outer:
            conn = self._require()
            if not outer:
                yield conn
                return
            try:
                await conn.execute("BEGIN IMMEDIATE")
            except BaseException:
                # cancelled (or failed) while the BEGIN round-trip was in
                # flight: the worker thread may still execute it, which
                # would leave the SHARED connection inside an open
                # transaction ("cannot start a transaction within a
                # transaction" on every later write). The ROLLBACK queues
                # behind the BEGIN on the same worker, so it always lands
                # after it; "no transaction is active" is harmless here.
                try:
                    await conn.execute("ROLLBACK")
                except BaseException:
                    LOG.debug("begin-abort rollback failed", exc_info=True)
                raise
            if TELEMETRY.enabled:
                _t0 = perf_counter()
                _ok = True
                try:
                    yield conn
                    _tc = perf_counter()
                    await conn.execute("COMMIT")
                    TELEMETRY.record("commit", perf_counter() - _tc)
                except BaseException:
                    _ok = False
                    try:
                        await conn.execute("ROLLBACK")
                    except Exception:
                        LOG.debug("rollback failed", exc_info=True)
                    raise
                finally:
                    TELEMETRY.record("transaction", perf_counter() - _t0, ok=_ok)
                return
            try:
                yield conn
                await conn.execute("COMMIT")
            except BaseException:
                try:
                    await conn.execute("ROLLBACK")
                except Exception:
                    LOG.debug("rollback failed", exc_info=True)
                raise

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        """Read access that never observes another task's open transaction."""
        async with self._guard.hold():
            yield self._require()

    @asynccontextmanager
    async def serialized(self) -> AsyncIterator[aiosqlite.Connection]:
        """Guard-only access: serialized, but NO transaction management.

        For callers that manage transactions themselves in the SQL text
        (e.g. migration scripts with their own BEGIN IMMEDIATE..COMMIT);
        write() would open a BEGIN IMMEDIATE that the script's own COMMIT
        closes, and the outer COMMIT would then fail.
        """
        async with self._guard.hold():
            yield self._require()


__all__ = ["PRAGMAS", "DatabaseConnection", "DatabaseError"]
