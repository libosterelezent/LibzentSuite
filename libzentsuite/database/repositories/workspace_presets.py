"""WorkspacePresetsRepository — P8's definition + execution store.

A preset DEFINITION (one per (workspace, name)) references
(account, plugin, profile) tuples — the body is orchestration metadata
(order, coordination rules), NEVER copied profile configuration. An
EXECUTION is one running/recoverable instance: it carries the workflow
checkpoints (completed participants) so a resumed run skips them
instead of replaying.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .base import BaseRepository, inserted_rowid, row_to_dict

__all__ = ["WorkspacePresetsRepository"]


class WorkspacePresetsRepository(BaseRepository):
    """CRUD for preset definitions + execution instances (P8)."""

    async def define(self, workspace: str, name: str, definition: dict[str, Any]) -> int:
        """Create or replace the (workspace, name) definition; returns its id."""
        now = time.time()
        existing = await self._fetch_one(
            "SELECT id FROM workspace_preset_definitions WHERE workspace=? AND name=?",
            (workspace, name),
        )
        async with self._tx(f"define preset {workspace}/{name}") as conn:
            if existing is not None:
                await conn.execute(
                    "UPDATE workspace_preset_definitions SET definition_json=?,"
                    " updated_at=? WHERE id=?",
                    (json.dumps(definition), now, int(existing["id"])),
                )
                return int(existing["id"])
            return inserted_rowid(
                await conn.execute(
                    "INSERT INTO workspace_preset_definitions"
                    " (workspace, name, definition_json, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (workspace, name, json.dumps(definition), now, now),
                )
            )

    async def get(self, preset_id: int) -> dict[str, Any] | None:
        row = await self._fetch_one(
            "SELECT * FROM workspace_preset_definitions WHERE id=?", (preset_id,)
        )
        if row is None:
            return None
        data = row_to_dict(row)
        data["definition"] = json.loads(data.pop("definition_json") or "{}")
        return data

    async def start_execution(self, workspace: str, preset_id: int, execution_id: str) -> None:
        """One running instance of the definition (idempotent re-start)."""
        now = time.time()
        async with self._tx(f"start execution {execution_id}") as conn:
            await conn.execute(
                "INSERT INTO workspace_executions"
                " (id, workspace, preset_id, state, checkpoint_json, created_at, updated_at)"
                " VALUES (?, ?, ?, 'running', '{}', ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET state='running', updated_at=excluded.updated_at",
                (execution_id, workspace, preset_id, now, now),
            )

    async def checkpoint(self, execution_id: str, completed: list[str]) -> None:
        """Persist the completed-participant set (the resume point)."""
        now = time.time()
        async with self._tx(f"checkpoint {execution_id}") as conn:
            await conn.execute(
                "UPDATE workspace_executions SET checkpoint_json=?, updated_at=? WHERE id=?",
                (json.dumps({"completed": completed}), now, execution_id),
            )

    async def execution(self, execution_id: str) -> dict[str, Any] | None:
        row = await self._fetch_one(
            "SELECT * FROM workspace_executions WHERE id=?", (execution_id,)
        )
        if row is None:
            return None
        data = row_to_dict(row)
        cp = json.loads(data.pop("checkpoint_json") or "{}")
        data["completed"] = list(cp.get("completed") or [])
        return data
