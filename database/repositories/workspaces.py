"""WorkspaceRepository — persistence for the workspace orchestration
projection (W2; supersedes FleetRepository at W5).

Persistence ONLY: no policy, no transition logic (that is
core.workspace's job through its validators). The W3 service commits
through this layer exactly as the fleet service did — single writer,
persist-first, projection maintained in the same transaction.

Membership note (decision 11): ``workspace_members`` rows are the
PROJECTION — every write here happens in the same transaction as the
``accounts.workspace`` pointer update the service performs; nothing
outside that path may mutate them.
"""

from __future__ import annotations

import time
from typing import Any

from database.models import (
    DeletionJobRecord,
    WorkspaceCooldownRecord,
    WorkspaceMemberRecord,
    WorkspaceState,
    WorkspaceTaskRecord,
    WorkspaceTransitionRecord,
)

from .base import BaseRepository

__all__ = ["WorkspaceRepository"]


def _state(row: Any) -> WorkspaceState:
    return WorkspaceState(
        name=row["name"],
        description=row["description"],
        enabled=bool(row["enabled"]),
        paused=bool(row["paused"]),
        generation=int(row["generation"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _member(row: Any) -> WorkspaceMemberRecord:
    return WorkspaceMemberRecord(
        account=row["account"],
        workspace=row["workspace"],
        mode=row["mode"],
        eligibility=row["eligibility"],
        joined_at=float(row["joined_at"]),
        updated_at=float(row["updated_at"]),
        version=int(row["version"]),
    )


def _task(row: Any) -> WorkspaceTaskRecord:
    return WorkspaceTaskRecord(
        id=row["id"],
        workspace=row["workspace"],
        task_id=row["task_id"],
        account=row["account"],
        status=row["status"],
        priority=int(row["priority"]),
        chain_run_id=row["chain_run_id"],
        step=row["step"],
        generation=int(row["generation"]),
        error=row["error"],
        resume_policy=row["resume_policy"],
        started_at=row["started_at"],
        stopped_at=row["stopped_at"],
        version=int(row["version"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _cooldown(row: Any) -> WorkspaceCooldownRecord:
    return WorkspaceCooldownRecord(
        id=int(row["id"]),
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        reason=row["reason"],
        started_at=float(row["started_at"]),
        expires_at=float(row["expires_at"]),
        source=row["source"],
        cleared_at=row["cleared_at"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _transition(row: Any) -> WorkspaceTransitionRecord:
    return WorkspaceTransitionRecord(
        id=int(row["id"]),
        kind=row["kind"],
        workspace=row["workspace"],
        generation=int(row["generation"]),
        reason=row["reason"],
        correlation_id=row["correlation_id"],
        occurred_at=float(row["occurred_at"]),
        task_id=row["task_id"],
        run_id=row["run_id"],
        account=row["account"],
    )


def _deletion_job(row: Any) -> DeletionJobRecord:
    return DeletionJobRecord(
        account=row["account"],
        state=row["state"],
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        created_at=float(row["created_at"]),
        completed_at=row["completed_at"],
    )


class WorkspaceRepository(BaseRepository):
    """Workspaces, member projection, tasks, cooldowns, history, jobs."""

    # ---------------------------------------------------------- workspaces
    async def ensure_workspace(self, name: str) -> WorkspaceState:
        """Get-or-create the workspace row (identity defaults)."""
        existing = await self.workspace_state(name)
        if existing is not None:
            return existing
        now = time.time()
        async with self._tx(f"ensure workspace {name!r}") as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO workspaces (name, created_at, updated_at) VALUES (?, ?, ?)",
                (name, now, now),
            )
        state = await self.workspace_state(name)
        assert state is not None
        return state

    async def workspace_state(self, name: str) -> WorkspaceState | None:
        row = await self._fetch_one("SELECT * FROM workspaces WHERE name = ?", (name,))
        return _state(row) if row is not None else None

    async def workspace_states(self) -> list[WorkspaceState]:
        rows = await self._fetch_all("SELECT * FROM workspaces ORDER BY name")
        return [_state(r) for r in rows]

    async def set_workspace(
        self,
        name: str,
        *,
        paused: bool,
        bump_generation: bool = True,
    ) -> WorkspaceState:
        """Set the pause flag (bumps generation by default — a pause
        change invalidates approved-not-dispatched work)."""
        now = time.time()
        async with self._tx(f"update workspace {name!r}") as conn:
            await conn.execute(
                "UPDATE workspaces SET paused = ?, updated_at = ?,"
                " generation = generation + CASE WHEN ? THEN 1 ELSE 0 END"
                " WHERE name = ?",
                (int(bool(paused)), now, int(bool(bump_generation)), name),
            )
        state = await self.workspace_state(name)
        assert state is not None
        return state

    async def bump_generation(self, name: str) -> WorkspaceState:
        """Bump the generation (the stale-dispatch fence)."""
        now = time.time()
        async with self._tx(f"bump workspace {name!r} generation") as conn:
            await conn.execute(
                "UPDATE workspaces SET generation = generation + 1, updated_at = ? WHERE name = ?",
                (now, name),
            )
        state = await self.workspace_state(name)
        assert state is not None
        return state

    # -------------------------------------------------- member projection
    async def upsert_member(self, member: WorkspaceMemberRecord) -> None:
        """Insert or update the projection row (same transaction as the
        pointer write — the W3 service's job, never a direct caller)."""
        now = time.time()
        async with self._tx(f"upsert workspace member {member.account!r}") as conn:
            await conn.execute(
                "INSERT INTO workspace_members (account, workspace, mode,"
                " eligibility, joined_at, updated_at, version)"
                " VALUES (?, ?, ?, ?, ?, ?, 1)"
                " ON CONFLICT(account) DO UPDATE SET"
                " workspace = excluded.workspace, mode = excluded.mode,"
                " eligibility = excluded.eligibility,"
                " updated_at = excluded.updated_at,"
                " version = workspace_members.version + 1",
                (
                    member.account,
                    member.workspace,
                    member.mode,
                    member.eligibility,
                    member.joined_at or now,
                    now,
                ),
            )

    async def member(self, account: str) -> WorkspaceMemberRecord | None:
        row = await self._fetch_one("SELECT * FROM workspace_members WHERE account = ?", (account,))
        return _member(row) if row is not None else None

    async def members(self, workspace: str | None = None) -> list[WorkspaceMemberRecord]:
        if workspace is None:
            rows = await self._fetch_all("SELECT * FROM workspace_members ORDER BY account")
        else:
            rows = await self._fetch_all(
                "SELECT * FROM workspace_members WHERE workspace = ? ORDER BY joined_at, account",
                (workspace,),
            )
        return [_member(r) for r in rows]

    async def delete_member(self, account: str) -> None:
        async with self._tx(f"delete workspace member {account!r}") as conn:
            await conn.execute("DELETE FROM workspace_members WHERE account = ?", (account,))

    # -------------------------------------------------------------- tasks
    async def insert_task(self, record: WorkspaceTaskRecord) -> None:
        async with self._tx(f"insert workspace task {record.id!r}") as conn:
            await conn.execute(
                "INSERT INTO workspace_tasks (id, workspace, task_id, account,"
                " status, priority, chain_run_id, step, generation, error,"
                " resume_policy, started_at, stopped_at, version, created_at,"
                " updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.workspace,
                    record.task_id,
                    record.account,
                    record.status,
                    record.priority,
                    record.chain_run_id,
                    record.step,
                    record.generation,
                    record.error,
                    record.resume_policy,
                    record.started_at,
                    record.stopped_at,
                    record.version,
                    record.created_at,
                    record.updated_at,
                ),
            )

    async def task(self, task_row_id: str) -> WorkspaceTaskRecord | None:
        row = await self._fetch_one("SELECT * FROM workspace_tasks WHERE id = ?", (task_row_id,))
        return _task(row) if row is not None else None

    async def tasks(
        self,
        *,
        workspace: str | None = None,
        status: str | None = None,
        account: str | None = None,
    ) -> list[WorkspaceTaskRecord]:
        sql = "SELECT * FROM workspace_tasks WHERE 1=1"
        params: list[Any] = []
        if workspace is not None:
            sql += " AND workspace = ?"
            params.append(workspace)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if account is not None:
            sql += " AND account = ?"
            params.append(account)
        sql += " ORDER BY created_at, id"
        rows = await self._fetch_all(sql, tuple(params))
        return [_task(r) for r in rows]

    async def update_task(self, task_row_id: str, **fields: Any) -> None:
        """Update mutable columns; bumps version (optimistic-write guard)."""
        allowed = {
            "status",
            "account",
            "started_at",
            "stopped_at",
            "generation",
            "error",
            "priority",
            "resume_policy",
            "chain_run_id",
            "step",
        }
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(f"unknown workspace task fields: {unknown}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        params = [*fields.values(), time.time(), task_row_id]
        async with self._tx(f"update workspace task {task_row_id!r}") as conn:
            await conn.execute(
                f"UPDATE workspace_tasks SET {assignments},"
                " updated_at = ?, version = version + 1 WHERE id = ?",
                tuple(params),
            )

    # ---------------------------------------------------------- cooldowns
    async def insert_cooldown(
        self,
        scope_type: str,
        scope_id: str,
        reason: str,
        started_at: float,
        expires_at: float,
        source: str,
    ) -> int:
        now = time.time()
        async with self._tx("insert workspace cooldown") as conn:
            cur = await conn.execute(
                "INSERT INTO workspace_cooldowns"
                " (scope_type, scope_id, reason, started_at, expires_at,"
                "  source, cleared_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (scope_type, scope_id, reason, started_at, expires_at, source, now, now),
            )
            return int(cur.lastrowid)

    async def cooldown(self, cooldown_id: int) -> WorkspaceCooldownRecord | None:
        row = await self._fetch_one(
            "SELECT * FROM workspace_cooldowns WHERE id = ?", (cooldown_id,)
        )
        return _cooldown(row) if row is not None else None

    async def cooldowns(
        self,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        active_only: bool = False,
        now: float | None = None,
    ) -> list[WorkspaceCooldownRecord]:
        sql = "SELECT * FROM workspace_cooldowns WHERE 1=1"
        params: list[Any] = []
        if scope_type is not None:
            sql += " AND scope_type = ?"
            params.append(scope_type)
        if scope_id is not None:
            sql += " AND scope_id = ?"
            params.append(scope_id)
        if active_only:
            sql += " AND cleared_at IS NULL AND expires_at > ?"
            params.append(now if now is not None else time.time())
        sql += " ORDER BY expires_at"
        rows = await self._fetch_all(sql, tuple(params))
        return [_cooldown(r) for r in rows]

    async def clear_cooldown(self, cooldown_id: int, cleared_at: float) -> None:
        async with self._tx(f"clear workspace cooldown {cooldown_id}") as conn:
            await conn.execute(
                "UPDATE workspace_cooldowns SET cleared_at = ?, updated_at = ? WHERE id = ?",
                (cleared_at, time.time(), cooldown_id),
            )

    # ------------------------------------------------------------ history
    async def append_transition(
        self,
        kind: str,
        workspace: str,
        generation: int,
        reason: str,
        correlation_id: str,
        occurred_at: float,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        account: str | None = None,
    ) -> None:
        async with self._tx(f"append workspace transition {kind}") as conn:
            await conn.execute(
                "INSERT INTO workspace_transitions"
                " (kind, workspace, generation, reason, correlation_id,"
                "  occurred_at, task_id, run_id, account)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    workspace,
                    generation,
                    reason,
                    correlation_id,
                    occurred_at,
                    task_id,
                    run_id,
                    account,
                ),
            )

    async def transitions(
        self, *, workspace: str | None = None, limit: int = 200
    ) -> list[WorkspaceTransitionRecord]:
        if workspace is None:
            rows = await self._fetch_all(
                "SELECT * FROM workspace_transitions ORDER BY id DESC LIMIT ?", (int(limit),)
            )
        else:
            rows = await self._fetch_all(
                "SELECT * FROM workspace_transitions WHERE workspace = ? ORDER BY id DESC LIMIT ?",
                (workspace, int(limit)),
            )
        return [_transition(r) for r in rows]

    # ------------------------------------------------------ deletion jobs
    async def create_deletion_job(self, account: str) -> DeletionJobRecord | None:
        """Durable step 1 of the cross-store workflow (decision 18)."""
        now = time.time()
        async with self._tx(f"create deletion job {account!r}") as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO deletion_jobs (account, state, created_at)"
                " VALUES (?, 'pending', ?)",
                (account, now),
            )
        row = await self._fetch_one("SELECT * FROM deletion_jobs WHERE account = ?", (account,))
        return _deletion_job(row) if row is not None else None

    async def deletion_job(self, account: str) -> DeletionJobRecord | None:
        row = await self._fetch_one("SELECT * FROM deletion_jobs WHERE account = ?", (account,))
        return _deletion_job(row) if row is not None else None

    async def deletion_jobs(self, *, state: str | None = None) -> list[DeletionJobRecord]:
        if state is None:
            rows = await self._fetch_all("SELECT * FROM deletion_jobs ORDER BY created_at")
        else:
            rows = await self._fetch_all(
                "SELECT * FROM deletion_jobs WHERE state = ? ORDER BY created_at", (state,)
            )
        return [_deletion_job(r) for r in rows]

    async def update_deletion_job(self, account: str, *, state: str, last_error: str = "") -> None:
        async with self._tx(f"update deletion job {account!r}") as conn:
            await conn.execute(
                "UPDATE deletion_jobs SET state = ?, attempts = attempts + 1,"
                " last_error = ?, completed_at ="
                " CASE WHEN ? IN ('complete', 'quarantined') THEN ? ELSE NULL END"
                " WHERE account = ?",
                (state, last_error, state, time.time(), account),
            )
