"""TaskHistoryRepository — persistent finished-task archive (spec §6).

Rows live in ``task_history``; the scheduler writes one row per finished
task (any terminal status) via ``add`` and may amend it via ``update``
(e.g. a late timeout reclassification). The scheduler's live Task ids are
preserved: an entry carrying its own "id" is stored under that id.

Rows map to/from plain dicts in the Task.as_dict() shape; unknown keys are
ignored, and "result_summary" is accepted as an alias for "summary"
(both spellings circulate in the contract).
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from core.errors import DatabaseError

from .base import BaseRepository

__all__ = ["TaskHistoryRepository"]

#: whitelisted task_history columns (also guards the f-string UPDATE)
_TASK_COLUMNS = (
    "id",
    "name",
    "account",
    "plugin",
    "action",
    "status",
    "priority",
    "submitted_at",
    "started_at",
    "finished_at",
    "attempts",
    "duration_s",
    "error",
    "summary",
)


class TaskHistoryRepository(BaseRepository):
    """add/update/recent for the task history archive (bounded: newest
    ``keep`` rows; 0 disables pruning)."""

    def __init__(self, database: Any, keep: int = 0) -> None:
        super().__init__(database)
        self._keep = max(0, int(keep))

    async def add(self, entry: dict[str, Any]) -> int:
        """Insert one history row; returns its id.

        Only whitelisted columns are taken from ``entry`` (extra keys are
        ignored); "result_summary" is mapped to "summary". NOT NULL columns
        fall back to sane defaults ('?', 'queued', now) so a thin dict still
        persists. When the entry carries its own "id" it is preserved and
        returned — the scheduler keeps its Task ids stable this way.

        Raises DatabaseError for a non-dict entry or an id collision.
        """
        if not isinstance(entry, dict):
            raise DatabaseError(f"task history entry must be a dict, got {type(entry).__name__}")
        row: dict[str, Any] = {}
        for col in _TASK_COLUMNS:
            if entry.get(col) is not None:
                row[col] = entry[col]
        if "summary" not in row and entry.get("result_summary") is not None:
            row["summary"] = entry["result_summary"]
        row.setdefault("account", "?")
        row.setdefault("action", "?")
        row.setdefault("status", "queued")
        row.setdefault("priority", 1)
        row.setdefault("attempts", 0)
        row.setdefault("submitted_at", time.time())

        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        async with self._tx("append task history") as conn:
            try:
                async with conn.execute(
                    f"INSERT INTO task_history ({columns}) VALUES ({placeholders})",
                    tuple(row.values()),
                ) as cur:
                    lastrowid = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise DatabaseError(
                    f"task history row for id {row.get('id')!r} already exists"
                ) from exc
            if self._keep:
                await self._prune_newest(conn, "task_history", self._keep)
            return int(row["id"]) if "id" in row else lastrowid

    async def max_id(self) -> int:
        """Highest stored task id (0 when the archive is empty)."""
        row = await self._fetch_one(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_history",
            what="read task history high-water mark",
        )
        return int(row["m"]) if row is not None else 0

    async def update(self, task_id: int, **fields: Any) -> None:
        """Update ONLY the provided columns of one history row.

        "result_summary" is accepted as the "summary" alias. Raises
        DatabaseError for an unknown field name (typos must be loud — they
        would otherwise silently persist nothing), for attempts to change
        "id", or when no row exists for task_id.
        """
        if not fields:
            return  # nothing requested — nothing to do (no error)
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            col = "summary" if key == "result_summary" else key
            if col == "id":
                raise DatabaseError("task history id cannot be changed")
            if col not in _TASK_COLUMNS:
                raise DatabaseError(
                    f"unknown task history field {key!r} (supports: {', '.join(_TASK_COLUMNS)})"
                )
            updates[col] = value
        assignments = ", ".join(f"{col} = ?" for col in updates)
        async with (
            self._tx(f"update task history {task_id}") as conn,
            conn.execute(
                f"UPDATE task_history SET {assignments} WHERE id = ?",
                (*updates.values(), task_id),
            ) as cur,
        ):
            if cur.rowcount == 0:
                raise DatabaseError(f"no task history row for id {task_id}")

    async def recent(self, limit: int = 50, account: str | None = None) -> list[dict[str, Any]]:
        """The newest ``limit`` rows (oldest-first), optionally per-account.

        Same DESC-fetch-then-reverse tail ordering as the audit trail, so
        every "recent" in this package behaves identically.
        """
        limit = max(0, int(limit))
        if account:
            rows = await self._fetch_all(
                "SELECT * FROM task_history WHERE account = ? ORDER BY id DESC LIMIT ?",
                (account, limit),
                what="read task history",
            )
        else:
            rows = await self._fetch_all(
                "SELECT * FROM task_history ORDER BY id DESC LIMIT ?",
                (limit,),
                what="read task history",
            )
        return [dict(row) for row in reversed(rows)]
