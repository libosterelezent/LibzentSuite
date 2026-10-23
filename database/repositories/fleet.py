"""FleetRepository — persistence for the fleet orchestration projection.

PHASE 9 (D2 database-authoritative): membership stays with
SyncGroupsRepository; this layer persists what the orchestrator owns —
per-group fleet state (pause + generation), per-member mode/eligibility,
task assignments, scoped cooldowns, and the append-only transition
history. Persistence ONLY: no policy, no transitions logic (that is
core.fleet's job through its validators).
"""
from __future__ import annotations

import time
from typing import Any, List, Optional, Sequence

from database.models import (
    FleetAssignmentRecord,
    FleetCooldownRecord,
    FleetGroupState,
    FleetMemberRecord,
    FleetTransitionRecord,
)

from .base import BaseRepository

__all__ = ["FleetRepository"]


def _group_state(row) -> FleetGroupState:
    return FleetGroupState(
        group_name=row["group_name"],
        paused=bool(row["paused"]),
        generation=int(row["generation"]),
        updated_at=float(row["updated_at"]),
    )


def _member(row) -> FleetMemberRecord:
    return FleetMemberRecord(
        account=row["account"],
        group_name=row["group_name"],
        mode=row["mode"],
        eligibility=row["eligibility"],
        since=float(row["since"]),
        version=int(row["version"]),
    )


def _assignment(row) -> FleetAssignmentRecord:
    return FleetAssignmentRecord(
        id=row["id"],
        group_name=row["group_name"],
        task_id=row["task_id"],
        status=row["status"],
        account=row["account"],
        priority=int(row["priority"]),
        created_at=float(row["created_at"]),
        started_at=row["started_at"],
        stopped_at=row["stopped_at"],
        resume_policy=row["resume_policy"] or "",
        version=int(row["version"]),
        generation=int(row["generation"]),
        error=row["error"],
    )


def _cooldown(row) -> FleetCooldownRecord:
    return FleetCooldownRecord(
        id=int(row["id"]),
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        reason=row["reason"],
        started_at=float(row["started_at"]),
        expires_at=float(row["expires_at"]),
        source=row["source"],
        cleared_at=row["cleared_at"],
    )


def _transition(row) -> FleetTransitionRecord:
    return FleetTransitionRecord(
        id=int(row["id"]),
        kind=row["kind"],
        group_name=row["group_name"],
        generation=int(row["generation"]),
        reason=row["reason"],
        correlation_id=row["correlation_id"],
        occurred_at=float(row["occurred_at"]),
        task_id=row["task_id"],
        assignment_id=row["assignment_id"],
        account=row["account"],
    )


class FleetRepository(BaseRepository):
    """CRUD for the fleet projection tables (v9 schema)."""

    # ---------------------------------------------------------- group state

    async def ensure_group(self, group_name: str) -> FleetGroupState:
        """Get-or-create the fleet state row (paused=0, generation=1)."""
        now = time.time()
        async with self._tx(f"ensure fleet group {group_name!r}") as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO fleet_groups"
                " (group_name, paused, generation, updated_at)"
                " VALUES (?, 0, 1, ?)",
                (group_name, now),
            )
        state = await self.group_state(group_name)
        assert state is not None   # just inserted; read-back cannot miss
        return state

    async def group_state(self, group_name: str) -> Optional[FleetGroupState]:
        row = await self._fetch_one(
            "SELECT * FROM fleet_groups WHERE group_name = ?",
            (group_name,), what="read fleet group state")
        return _group_state(row) if row else None

    async def set_group(self, group_name: str, *, paused: bool,
                        bump_generation: bool = True) -> FleetGroupState:
        """Set the pause flag (bumps generation by default — a pause
        change invalidates approved-not-dispatched work, spec §5).
        Returns the new state."""
        now = time.time()
        async with self._tx(f"update fleet group {group_name!r}") as conn:
            await conn.execute(
                "UPDATE fleet_groups SET paused = ?, updated_at = ?,"
                " generation = generation + CASE WHEN ? THEN 1 ELSE 0 END"
                " WHERE group_name = ?",
                (int(bool(paused)), now, int(bool(bump_generation)), group_name),
            )
        state = await self.group_state(group_name)
        assert state is not None
        return state

    async def bump_generation(self, group_name: str) -> FleetGroupState:
        """Bump ONLY the generation counter — membership changes fence
        stale dispatches without touching the pause flag."""
        now = time.time()
        async with self._tx(f"bump fleet generation {group_name!r}") as conn:
            await conn.execute(
                "UPDATE fleet_groups SET updated_at = ?,"
                " generation = generation + 1 WHERE group_name = ?",
                (now, group_name),
            )
        state = await self.group_state(group_name)
        assert state is not None
        return state

    async def group_states(self) -> List[FleetGroupState]:
        rows = await self._fetch_all(
            "SELECT * FROM fleet_groups ORDER BY group_name",
            what="read fleet group states")
        return [_group_state(r) for r in rows]

    # -------------------------------------------------------------- members

    async def upsert_member(self, member: FleetMemberRecord) -> None:
        async with self._tx(f"upsert fleet member {member.account!r}") as conn:
            await conn.execute(
                "INSERT INTO fleet_members"
                " (account, group_name, mode, eligibility, since, version)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(account) DO UPDATE SET"
                "  group_name = excluded.group_name,"
                "  mode = excluded.mode,"
                "  eligibility = excluded.eligibility,"
                "  since = excluded.since,"
                "  version = fleet_members.version + 1",
                (member.account, member.group_name, member.mode,
                 member.eligibility, member.since, member.version),
            )

    async def member(self, account: str) -> Optional[FleetMemberRecord]:
        row = await self._fetch_one(
            "SELECT * FROM fleet_members WHERE account = ?", (account,),
            what="read fleet member")
        return _member(row) if row else None

    async def members(self, group_name: Optional[str] = None
                      ) -> List[FleetMemberRecord]:
        if group_name is None:
            rows = await self._fetch_all(
                "SELECT * FROM fleet_members ORDER BY account",
                what="read fleet members")
        else:
            rows = await self._fetch_all(
                "SELECT * FROM fleet_members WHERE group_name = ?"
                " ORDER BY account", (group_name,),
                what="read fleet members")
        return [_member(r) for r in rows]

    async def delete_member(self, account: str) -> None:
        async with self._tx(f"delete fleet member {account!r}") as conn:
            await conn.execute(
                "DELETE FROM fleet_members WHERE account = ?", (account,))

    # ---------------------------------------------------------- assignments

    async def insert_assignment(self, rec: FleetAssignmentRecord) -> None:
        async with self._tx(f"insert fleet assignment {rec.id!r}") as conn:
            await conn.execute(
                "INSERT INTO fleet_assignments"
                " (id, group_name, task_id, account, status, priority,"
                "  created_at, started_at, stopped_at, resume_policy,"
                "  version, generation, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.id, rec.group_name, rec.task_id, rec.account, rec.status,
                 rec.priority, rec.created_at, rec.started_at, rec.stopped_at,
                 rec.resume_policy, rec.version, rec.generation, rec.error),
            )

    async def assignment(self, assignment_id: str
                         ) -> Optional[FleetAssignmentRecord]:
        row = await self._fetch_one(
            "SELECT * FROM fleet_assignments WHERE id = ?", (assignment_id,),
            what="read fleet assignment")
        return _assignment(row) if row else None

    async def assignments(self, *, group_name: Optional[str] = None,
                          status: Optional[str] = None,
                          account: Optional[str] = None
                          ) -> List[FleetAssignmentRecord]:
        sql = "SELECT * FROM fleet_assignments WHERE 1=1"
        params: List[Any] = []
        if group_name is not None:
            sql += " AND group_name = ?"
            params.append(group_name)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if account is not None:
            sql += " AND account = ?"
            params.append(account)
        sql += " ORDER BY created_at, id"
        rows = await self._fetch_all(sql, tuple(params),
                                     what="read fleet assignments")
        return [_assignment(r) for r in rows]

    async def update_assignment(self, assignment_id: str, **fields: Any) -> None:
        """Update mutable columns; bumps version (optimistic-write guard)."""
        allowed = {"status", "account", "started_at", "stopped_at",
                   "generation", "error", "priority", "resume_policy"}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(
                f"unknown fleet assignment field(s) {unknown} "
                f"(allowed: {sorted(allowed)})")
        sets = ", ".join(f"{name} = ?" for name in fields)
        params: List[Any] = list(fields.values())
        params.append(assignment_id)
        async with self._tx(f"update fleet assignment {assignment_id!r}") as conn:
            await conn.execute(
                f"UPDATE fleet_assignments SET {sets},"
                " version = version + 1 WHERE id = ?",
                tuple(params),
            )

    # ------------------------------------------------------------ cooldowns

    async def insert_cooldown(self, scope_type: str, scope_id: str,
                              reason: str, started_at: float,
                              expires_at: float, source: str) -> int:
        async with self._tx("insert fleet cooldown") as conn:
            cur = await conn.execute(
                "INSERT INTO fleet_cooldowns"
                " (scope_type, scope_id, reason, started_at, expires_at,"
                "  source, cleared_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (scope_type, scope_id, reason, started_at, expires_at,
                 source),
            )
            return int(cur.lastrowid)

    async def cooldown(self, cooldown_id: int) -> Optional[FleetCooldownRecord]:
        row = await self._fetch_one(
            "SELECT * FROM fleet_cooldowns WHERE id = ?", (cooldown_id,),
            what="read fleet cooldown")
        return _cooldown(row) if row else None

    async def cooldowns(self, *, scope_type: Optional[str] = None,
                        scope_id: Optional[str] = None,
                        active_only: bool = False,
                        now: Optional[float] = None
                        ) -> List[FleetCooldownRecord]:
        sql = "SELECT * FROM fleet_cooldowns WHERE 1=1"
        params: List[Any] = []
        if scope_type is not None:
            sql += " AND scope_type = ?"
            params.append(scope_type)
        if scope_id is not None:
            sql += " AND scope_id = ?"
            params.append(scope_id)
        if active_only:
            moment = now if now is not None else time.time()
            sql += " AND cleared_at IS NULL AND expires_at > ?"
            params.append(moment)
        sql += " ORDER BY expires_at"
        rows = await self._fetch_all(sql, tuple(params),
                                     what="read fleet cooldowns")
        return [_cooldown(r) for r in rows]

    async def clear_cooldown(self, cooldown_id: int,
                             cleared_at: float) -> None:
        async with self._tx(f"clear fleet cooldown {cooldown_id}") as conn:
            await conn.execute(
                "UPDATE fleet_cooldowns SET cleared_at = ? WHERE id = ?",
                (cleared_at, cooldown_id))

    # ----------------------------------------------------------- transitions

    async def append_transition(self, kind: str, group_name: str,
                                generation: int, reason: str,
                                correlation_id: str, occurred_at: float,
                                *, task_id: Optional[str] = None,
                                assignment_id: Optional[str] = None,
                                account: Optional[str] = None) -> None:
        async with self._tx("append fleet transition") as conn:
            await conn.execute(
                "INSERT INTO fleet_transitions"
                " (kind, group_name, generation, reason, correlation_id,"
                "  occurred_at, task_id, assignment_id, account)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, group_name, generation, reason, correlation_id,
                 occurred_at, task_id, assignment_id, account),
            )

    async def transitions(self, *, group_name: Optional[str] = None,
                          limit: int = 200) -> List[FleetTransitionRecord]:
        if group_name is None:
            rows = await self._fetch_all(
                "SELECT * FROM fleet_transitions ORDER BY id DESC LIMIT ?",
                (int(limit),), what="read fleet transitions")
        else:
            rows = await self._fetch_all(
                "SELECT * FROM fleet_transitions WHERE group_name = ?"
                " ORDER BY id DESC LIMIT ?", (group_name, int(limit)),
                what="read fleet transitions")
        return [_transition(r) for r in rows]
