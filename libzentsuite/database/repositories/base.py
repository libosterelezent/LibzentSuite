"""Shared plumbing for every repository over the async connection.

Repos never touch cursors, commits, or locks directly: reads go through
``_fetch_all``/``_fetch_one`` (WAL readers, no writer lock), writes go
through ``_tx`` (serialized transaction context). SQL stays inside repos;
core services consume repository methods; plugins consume plugin-facing
services — never this layer (spec §30/§31).
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from ..connection import DatabaseError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    import aiosqlite

    from libzentsuite.database.db import Database

LOG = logging.getLogger("libzent.database")

__all__ = ["BaseRepository", "loads_tolerant", "row_to_dict"]


def row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    """The one Row -> plain-dict boundary converter.

    ``dict(row)`` is untypeable on ``sqlite3.Row`` (it is not a Mapping in
    typeshed), so this comprehension is the canonical typed conversion
    every repository uses; column values stay whatever SQLite stored
    (str/int/float/bytes/None).
    """
    return {key: row[key] for key in tuple(row.keys())}


def inserted_rowid(cursor: aiosqlite.Cursor) -> int:
    """``lastrowid`` of a just-executed INSERT (typeshed types it optional).

    SQLite always allocates a rowid for an INSERT into a rowid table, so
    the assert documents that invariant rather than defending against it.
    """
    rowid = cursor.lastrowid
    assert rowid is not None, "INSERT must allocate a rowid"
    return rowid


#: sentinel default for callers that must tell a stored JSON ``null`` (a real
#: value) from a corrupt/missing payload (which returns the default)
CORRUPT = object()


def loads_tolerant(raw: str | None, default: Any, context: str) -> Any:
    """``json.loads`` with a safety net: corrupt/missing values return ``default``.

    Never raises — a damaged row must not take the framework down. Reads
    fall back to ``default`` (and warn so the damage is visible in the
    log); the next write replaces the bad payload.
    """
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        LOG.warning("corrupt JSON in %s (%s); using default %r", context, exc, default)
        return default


class BaseRepository:
    """Base class for all repositories: the shared async connection."""

    def __init__(self, database: Database) -> None:
        #: the owning Database service (provides ``.connection``)
        self._db = database

    # ------------------------------------------------------------------ reads
    async def _fetch_all(
        self, sql: str, params: Sequence[Any] = (), *, what: str = "read"
    ) -> list[aiosqlite.Row]:
        """SELECT -> all rows (WAL read; never takes the writer lock)."""
        try:
            return await self._db.connection.fetch_all(sql, params)
        except Exception as exc:
            raise DatabaseError(f"database {what} failed: {exc}") from exc

    async def _fetch_one(
        self, sql: str, params: Sequence[Any] = (), *, what: str = "read"
    ) -> aiosqlite.Row | None:
        """SELECT -> first row or None."""
        try:
            return await self._db.connection.fetch_one(sql, params)
        except Exception as exc:
            raise DatabaseError(f"database {what} failed: {exc}") from exc

    # ----------------------------------------------------------------- writes
    @asynccontextmanager
    async def _tx(self, what: str = "write") -> AsyncIterator[aiosqlite.Connection]:
        """One all-or-nothing write transaction (writers serialized).

        Use as ``async with self._tx("delete account") as conn:
        await conn.execute(...)``. Multiple statements commit or roll
        back together — that is what keeps cascading deletes atomic.
        """
        try:
            async with self._db.connection.write() as conn:
                yield conn
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"database {what} failed: {exc}") from exc

    async def _prune_newest(self, conn: aiosqlite.Connection, table: str, keep: int) -> None:
        """Keep only the newest ``keep`` rows (by id); call inside ``_tx``."""
        # Bandit B608: table is an internal constant supplied by repo
        # callers; the OFFSET value is bound as a parameter.
        await conn.execute(
            f"DELETE FROM {table} WHERE id <= "  # nosec B608
            f"(SELECT id FROM {table} ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (int(keep),),
        )
