"""PluginDataStore — AccountPluginData persistence (PHASE 10, B3 LOCKED).

SEPARATE DATABASE FLOW (operator decision 2026-10-22): the documents
live in their own WAL SQLite file ``state/plugin-data.db`` with a
SELF-INITIALIZING schema (CREATE IF NOT EXISTS at connect — no main
migrator involvement). Rationale (spec §7): the documents are standalone
copies with no relations to config truth, so nothing needs cross-DB
transactions, while churn isolation, independent backup/retention and
failure isolation all favor the split.

    plugin_data(account, plugin, guild_id, channel_id,
                data_json, version, updated_at)
    PRIMARY KEY (account, plugin, guild_id, channel_id)

Scope lattice (FIXED, option A): account -> plugin -> guild -> channel.
``guild_id``/``channel_id`` use the STRING ``"0"`` as the plugin-global
/ guild-level sentinel; every real id is a digit STRING (the snowflake
transport rule — JS rounds 19-digit numbers).

STANDALONE COPIES (no inheritance): channel A's document never inherits
channel B's — a missing document is absent, not a parent's data.

Optimistic writes: ``put`` with ``expected_version`` bumps version and
REFUSES a stale writer (VersionConflict) so two concurrent writers can
never silently clobber — last-writer-wins only when the caller opts out
by passing ``expected_version=None``.

Pure persistence: no policy, no fleet knowledge, game-agnostic
vocabulary only (§10b).
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

from libzentsuite.core.errors import PluginError

if TYPE_CHECKING:
    import builtins

__all__ = ["PLUGIN_GLOBAL", "PluginDataStore", "VersionConflict"]

LOG = logging.getLogger("libzent.plugin_data")

#: sentinel scope ids: "0" = plugin-global (guild row) / guild-level (channel row)
PLUGIN_GLOBAL = "0"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plugin_data (
    account    TEXT NOT NULL,
    plugin     TEXT NOT NULL,
    guild_id   TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    data_json  TEXT NOT NULL DEFAULT '{}',
    version    INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    PRIMARY KEY (account, plugin, guild_id, channel_id)
);
"""


class VersionConflict(PluginError):
    """An optimistic put raced a newer writer: the stored version moved
    past ``expected_version``. Refetch and re-apply — never a silent
    clobber."""


class PluginDataStore:
    """Owns ONE connection to state/plugin-data.db (separate from the
    main Database). The constructor does NO I/O; ``await connect()`` at
    boot; plugins never see this class — they get the scoped facade
    through ctx.services (AccountPluginServices)."""

    def __init__(self, path: str | Path = "state/plugin-data.db") -> None:
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------- boot

    async def connect(self) -> None:
        """Open the connection, apply pragmas, self-initialize the
        schema. Idempotent."""
        if self._conn is not None:
            return
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        if self._path != ":memory:":
            # same tightening discipline as the main database
            with suppress(OSError):
                os.chmod(self._path, 0o600)
        LOG.info("plugin-data store ready (%s)", self._path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def path(self) -> str:
        return self._path

    # ------------------------------------------------------------ helpers

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise PluginError("plugin-data store is not connected")
        return self._conn

    @staticmethod
    def _validate_scope(guild_id: str, channel_id: str) -> None:
        for name, value in (("guild_id", guild_id), ("channel_id", channel_id)):
            if not isinstance(value, str) or not (value == PLUGIN_GLOBAL or value.isdigit()):
                raise PluginError(
                    f"plugin-data {name} must be a digit string or '0' "
                    f"(snowflakes are STRINGS end to end), got {value!r}"
                )

    @staticmethod
    def _row_to_doc(row: Any) -> dict[str, Any]:
        return {
            "account": row[0],
            "plugin": row[1],
            "guild_id": row[2],
            "channel_id": row[3],
            "data": json.loads(row[4]) if row[4] else {},
            "version": int(row[5]),
            "updated_at": float(row[6]),
        }

    # -------------------------------------------------------------- reads

    async def get(
        self,
        account: str,
        plugin: str,
        *,
        guild_id: str = PLUGIN_GLOBAL,
        channel_id: str = PLUGIN_GLOBAL,
    ) -> dict[str, Any] | None:
        """One document (None when absent — standalone copies never
        fall through to a parent scope)."""
        self._validate_scope(guild_id, channel_id)
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT account, plugin, guild_id, channel_id, data_json,"
            " version, updated_at FROM plugin_data"
            " WHERE account=? AND plugin=? AND guild_id=? AND channel_id=?",
            (account, plugin, guild_id, channel_id),
        )
        row = await cur.fetchone()
        return self._row_to_doc(row) if row else None

    async def list(
        self,
        account: str,
        plugin: str | None = None,
        *,
        guild_id: str | None = None,
        channel_id: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Scope nodes matching the given filters (each row = one doc)."""
        conn = self._require_conn()
        sql = (
            "SELECT account, plugin, guild_id, channel_id, data_json,"
            " version, updated_at FROM plugin_data WHERE 1=1"
        )
        params: list[Any] = [account]
        sql += " AND account=?"
        if plugin is not None:
            sql += " AND plugin=?"
            params.append(plugin)
        if guild_id is not None:
            sql += " AND guild_id=?"
            params.append(guild_id)
        if channel_id is not None:
            sql += " AND channel_id=?"
            params.append(channel_id)
        sql += " ORDER BY plugin, guild_id, channel_id"
        cur = await conn.execute(sql, tuple(params))
        rows = await cur.fetchall()
        return [self._row_to_doc(r) for r in rows]

    # ------------------------------------------------------------- writes

    async def put(
        self,
        account: str,
        plugin: str,
        guild_id: str,
        channel_id: str,
        data: Any,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Write ONE document. ``expected_version`` enables the
        optimistic guard (VersionConflict on a racing writer); None
        means last-writer-wins (the dashboard's simple path)."""
        self._validate_scope(guild_id, channel_id)
        if not isinstance(data, dict):
            raise PluginError(
                f"plugin-data document must be a JSON object, got {type(data).__name__}"
            )
        try:
            payload = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise PluginError(f"plugin-data document is not JSON-able: {exc}") from exc
        conn = self._require_conn()
        now = time.time()

        existing = await self.get(account, plugin, guild_id=guild_id, channel_id=channel_id)
        if existing is None:
            if expected_version not in (None, 0):
                # the writer READ a document that is now gone (deleted by
                # a racing writer) — that is a conflict, not a recreate;
                # 0 = "I expect absent" is the explicit create-expectation
                raise VersionConflict(
                    f"plugin-data document ({plugin} {guild_id}/{channel_id}) "
                    f"was deleted by another writer "
                    f"(expected version {expected_version})"
                )
            await conn.execute(
                "INSERT INTO plugin_data (account, plugin, guild_id,"
                " channel_id, data_json, version, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 1, ?)",
                (account, plugin, guild_id, channel_id, payload, now),
            )
            await conn.commit()
            return {
                "account": account,
                "plugin": plugin,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "data": data,
                "version": 1,
                "updated_at": now,
            }

        if expected_version is not None and existing["version"] != expected_version:
            raise VersionConflict(
                f"plugin-data document ({plugin} {guild_id}/{channel_id}) "
                f"moved to version {existing['version']} "
                f"(expected {expected_version}) — refetch and re-apply"
            )
        await conn.execute(
            "UPDATE plugin_data SET data_json=?, version=version+1,"
            " updated_at=? WHERE account=? AND plugin=? AND guild_id=?"
            " AND channel_id=?",
            (payload, now, account, plugin, guild_id, channel_id),
        )
        await conn.commit()
        return {
            "account": account,
            "plugin": plugin,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "data": data,
            "version": existing["version"] + 1,
            "updated_at": now,
        }

    async def delete(
        self,
        account: str,
        plugin: str,
        *,
        guild_id: str = PLUGIN_GLOBAL,
        channel_id: str = PLUGIN_GLOBAL,
    ) -> bool:
        """Remove one document (True when it existed)."""
        self._validate_scope(guild_id, channel_id)
        conn = self._require_conn()
        cur = await conn.execute(
            "DELETE FROM plugin_data WHERE account=? AND plugin=? AND guild_id=? AND channel_id=?",
            (account, plugin, guild_id, channel_id),
        )
        await conn.commit()
        return cur.rowcount > 0

    async def drop_account(self, account: str) -> int:
        """Delete EVERY document keyed under one account.

        The cross-store deletion boundary (WORKSPACE_DESIGN §13.3,
        decision 18): account removal invokes this idempotently inside
        its durable deletion-job workflow — a re-created same-named
        account must never inherit the old one's documents (the gap
        found 2026-11-13). Returns rows removed; 0 is the normal
        second call (idempotent by construction)."""
        if not account or not str(account).strip():
            raise PluginError("drop_account needs a non-empty account name")
        conn = self._require_conn()
        cur = await conn.execute(
            "DELETE FROM plugin_data WHERE account=?",
            (str(account).strip(),),
        )
        await conn.commit()
        return int(cur.rowcount or 0)
