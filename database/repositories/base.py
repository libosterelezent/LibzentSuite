"""Shared plumbing for every repository (connection guard + error shaping).

All repositories share ONE sqlite3 connection owned by ``database.db.Database``;
every statement runs under that Database's RLock, which makes the repos safe
to call from any thread (the audited old implementation used the same
single-lock discipline through three audit rounds).

Error model (INTEGRATION.md ground rules):
  * any ``sqlite3.Error`` escaping a read/write is re-raised as
    ``core.errors.DatabaseError`` with a human message — callers above the
    persistence layer only ever see that one error type;
  * repositories catch *expected* ``sqlite3.IntegrityError`` duplicates
    INSIDE the transaction and re-raise as DatabaseError with a precise,
    human message (loud, but clean);
  * JSON columns are parsed tolerantly (``loads_tolerant``): a corrupt row
    logs a warning and yields a default instead of crashing the read —
    writes simply overwrite the damage on the next save.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional, Sequence

from core.errors import DatabaseError

LOG = logging.getLogger("slaughtermudae.database")

__all__ = ["BaseRepository", "loads_tolerant"]


def loads_tolerant(raw: Optional[str], default: Any, context: str) -> Any:
    """``json.loads`` with a safety net: corrupt/missing values return ``default``.

    Never raises — a damaged row must not take the framework down. Reads fall
    back to ``default`` (and warn once per occurrence so the damage is visible
    in the log); the next write replaces the bad payload.

    Raises: nothing (by design).
    """
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        LOG.warning("corrupt JSON in %s (%s); using default %r", context, exc, default)
        return default


class BaseRepository:
    """Base class for all repositories: the shared connection + lock.

    Subclasses get three guarded helpers:

    * ``_read`` / ``_read_one`` — SELECTs under the lock, errors translated;
    * ``_tx`` — one all-or-nothing write transaction (lock held throughout,
      COMMIT on success, ROLLBACK on error). Callers may catch
      ``sqlite3.IntegrityError`` inside the block to raise a friendlier
      DatabaseError; any other ``sqlite3.Error`` is translated at exit.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    # ------------------------------------------------------------------ reads
    def _read(self, sql: str, params: Sequence[Any] = (), *, what: str = "read") -> List[sqlite3.Row]:
        """Run a SELECT under the lock; returns all rows.

        Raises DatabaseError when the statement fails (bad SQL, closed DB...).
        """
        try:
            with self._lock:
                return self._conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"database {what} failed: {exc}") from exc

    def _read_one(
        self, sql: str, params: Sequence[Any] = (), *, what: str = "read"
    ) -> Optional[sqlite3.Row]:
        """Run a SELECT under the lock; returns the first row or None.

        Raises DatabaseError when the statement fails.
        """
        try:
            with self._lock:
                return self._conn.execute(sql, tuple(params)).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError(f"database {what} failed: {exc}") from exc

    # ----------------------------------------------------------------- writes
    @contextmanager
    def _tx(self, what: str = "write") -> Iterator[sqlite3.Connection]:
        """One all-or-nothing write transaction (RLock held throughout).

        Use as ``with self._tx("delete account") as conn: conn.execute(...)``.
        Multiple statements in one block commit or roll back together — that
        is what keeps cascading deletes atomic.

        Raises DatabaseError for any sqlite3 failure (after rollback), unless
        the body already raised something else (e.g. a friendly DatabaseError
        built from an IntegrityError).
        """
        with self._lock:
            try:
                with self._conn:
                    yield self._conn
            except sqlite3.Error as exc:
                raise DatabaseError(f"database {what} failed: {exc}") from exc

    def _prune_newest(self, table: str, keep: int) -> None:
        """Keep only the newest ``keep`` rows (by id) in ``table``.

        MUST be called inside an open ``_tx`` — pruning is part of the insert
        transaction so a crash can neither lose the new row nor over-prune.
        Ported verbatim (SQL shape) from the audited old implementation: when
        fewer than ``keep`` rows exist, the subquery yields NULL and the
        DELETE matches nothing.
        """
        self._conn.execute(
            f"DELETE FROM {table} WHERE id <= "
            f"(SELECT id FROM {table} ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (int(keep),),
        )
