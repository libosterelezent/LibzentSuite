"""FleetService — the ONE orchestration service (PHASE 9, rollout B).

Every fleet operation — from the dashboard, the CLI, or tests — flows
through this service (rollout B's acceptance criterion). The service is
the ONLY writer of the fleet projection: it validates through the Phase
A transition tables, persists FIRST (D2 database-authoritative,
write-ahead), appends the transition history, then publishes the state
change on the event seam (bus delivery is an optimization, spec §3).

Division of labour (spec §1 — none absorbs another):
  * membership itself stays with SyncGroupManager; the service flips
    the member's fleet MODE around it through injected seams;
  * pacing stays with the limiter; cooldowns here are POLICY records;
  * execution stays with the scheduler; assignments here are the work
    UNITS whose dispatch the scheduler performs (wired in rollout C).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, List, Optional

from .models import (
    AssignmentStatus,
    CooldownRecord,
    CooldownScopeType,
    CooldownSource,
    ExecutionMode,
    FleetEvent,
    MemberEligibility,
    TaskDefinition,
)
from .transitions import (
    validate_assignment_transition,
    validate_member_transition,
)

from database.models import (
    FleetAssignmentRecord,
    FleetCooldownRecord,
    FleetGroupState,
    FleetMemberRecord,
)

__all__ = ["FleetService"]

LOG = logging.getLogger("libzent.fleet")

#: async seams injected at construction (all optional; tests stub them,
#: bootstrap wires the real collaborators)
GroupExists = Callable[[str], Awaitable[bool]]
MembershipJoin = Callable[[str, str], Awaitable[None]]
MembershipLeave = Callable[[str], Awaitable[None]]
Publish = Callable[[FleetEvent], Any]


class FleetService:
    """Fleet state, eligibility, assignments, cooldowns — one service.

    The constructor does NO I/O; ``await load()`` at boot builds the
    in-memory projection from the repository (the database stays
    authoritative — every command commits before it publishes).
    """

    def __init__(
        self,
        fleet_repo: Any,
        *,
        group_exists: Optional[GroupExists] = None,
        membership_join: Optional[MembershipJoin] = None,
        membership_leave: Optional[MembershipLeave] = None,
        publisher: Optional[Publish] = None,
        time_fn: Optional[Callable[[], float]] = None,
        id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self._repo = fleet_repo
        self._group_exists = group_exists
        self._membership_join = membership_join
        self._membership_leave = membership_leave
        self._publisher = publisher
        self._now = time_fn or time.time
        self._new_id = id_factory or (lambda: uuid.uuid4().hex[:12])
        #: one command at a time: the projection and the repo rows stay
        #: in agreement without a per-entity lock zoo (spec §1: do not
        #: create a manager per noun)
        self._lock = asyncio.Lock()
        self._groups: dict = {}        # name -> FleetGroupState
        self._members: dict = {}       # account -> FleetMemberRecord
        self._assignments: dict = {}   # id -> FleetAssignmentRecord

    # ---------------------------------------------------------------- boot

    async def load(self) -> dict:
        """Projection boot: read back the authoritative rows."""
        async with self._lock:
            return await self._reload()

    async def _reload(self) -> dict:
        self._groups = {g.group_name: g
                        for g in await self._repo.group_states()}
        self._members = {m.account: m for m in await self._repo.members()}
        self._assignments = {a.id: a
                             for a in await self._repo.assignments()}
        return {"groups": len(self._groups),
                "members": len(self._members),
                "assignments": len(self._assignments)}

    # ------------------------------------------------------------- helpers

    def _require_member(self, account: str) -> FleetMemberRecord:
        member = self._members.get(account)
        if member is None:
            raise ValueError(
                f"account {account!r} is not a fleet member (join first)")
        return member

    def _require_group(self, group_name: str) -> FleetGroupState:
        state = self._groups.get(group_name)
        if state is None:
            raise ValueError(f"unknown fleet group {group_name!r}")
        return state

    def _require_assignment(self, assignment_id: str) -> FleetAssignmentRecord:
        rec = self._assignments.get(assignment_id)
        if rec is None:
            raise ValueError(f"unknown fleet assignment {assignment_id!r}")
        return rec

    async def _record(self, kind: str, group_name: str, reason: str, *,
                      task_id: Optional[str] = None,
                      assignment_id: Optional[str] = None,
                      account: Optional[str] = None) -> FleetEvent:
        """Persist-first state change: append history, then publish.

        History is the write-ahead record (D2); bus delivery after it
        is an optimization (spec §3) and never breaks the transition.
        """
        state = self._groups.get(group_name)
        generation = state.generation if state else 1
        event = FleetEvent(kind=kind, group_id=group_name,
                           generation=generation, reason=reason,
                           task_id=task_id, assignment_id=assignment_id,
                           account_id=account)
        await self._repo.append_transition(
            kind, group_name, event.generation, reason,
            event.correlation_id, event.occurred_at,
            task_id=task_id, assignment_id=assignment_id, account=account)
        if self._publisher is not None:
            try:
                result = self._publisher(event)
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                LOG.exception("fleet event publish failed (%s)", kind)
        return event

    def _group_for_scope(self, scope_type: str, scope_id: str) -> str:
        """Best-effort group name for a cooldown's history row: the
        scope's own group, else the account's group, else the bare id —
        history stays writable in every case."""
        if scope_type == CooldownScopeType.GROUP:
            return scope_id
        if scope_type == CooldownScopeType.ACCOUNT:
            member = self._members.get(scope_id)
            return member.group_name if member else scope_id
        return scope_id

    # ------------------------------------------------------------ join/leave

    async def join_group(self, account: str, group_name: str) -> FleetEvent:
        """§8 JOIN: validate -> GROUP_MANAGED -> member ELIGIBLE ->
        persist -> publish. Membership itself moves through the seam."""
        async with self._lock:
            if not isinstance(account, str) or not account.strip():
                raise ValueError("account must be a non-empty string")
            account = account.strip()
            if self._group_exists is not None and \
                    not await self._group_exists(group_name):
                raise ValueError(f"unknown sync group {group_name!r}")
            existing = self._members.get(account)
            if existing is not None:
                raise ValueError(
                    f"account {account!r} already manages with group "
                    f"{existing.group_name!r} (leave first — single-group "
                    f"membership is locked)")
            await self._repo.ensure_group(group_name)
            member = FleetMemberRecord(
                account=account, group_name=group_name,
                mode=ExecutionMode.GROUP_MANAGED,
                eligibility=MemberEligibility.ELIGIBLE,
                since=self._now())
            await self._repo.upsert_member(member)
            # a membership change invalidates approved-not-dispatched
            # work — bump the fence WITHOUT touching the pause flag
            await self._repo.bump_generation(group_name)
            if self._membership_join is not None:
                await self._membership_join(account, group_name)
            await self._reload()
            return await self._record("MemberJoined", group_name,
                                      f"{account} joined (group_managed)",
                                      account=account)

    async def leave_group(self, account: str) -> FleetEvent:
        """§8 LEAVE: release the account's work, retire the member,
        restore INDEPENDENT, publish. Stale dispatch is fenced by the
        generation bump, never by goodwill."""
        async with self._lock:
            member = self._require_member(account)
            group_name = member.group_name
            await self._release_live_work(account)
            validate_member_transition(member.eligibility,
                                       MemberEligibility.RETIRED, leaving=True)
            await self._repo.delete_member(account)
            await self._repo.bump_generation(group_name)
            if self._membership_leave is not None:
                await self._membership_leave(account)
            await self._reload()
            return await self._record("MemberLeft", group_name,
                                      f"{account} left (independent restored)",
                                      account=account)

    async def _release_live_work(self, account: str) -> None:
        """Return the account's live work to the group queue: assigned
        goes back to the queue for another member; running suspends
        (the in-flight drain wait is the scheduler's — rollout C/E).
        Caller holds the command lock."""
        for rec in await self._repo.assignments(account=account):
            if rec.status == AssignmentStatus.ASSIGNED:
                validate_assignment_transition(
                    rec.status, AssignmentStatus.QUEUED)
                await self._repo.update_assignment(
                    rec.id, status=AssignmentStatus.QUEUED, account=None)
            elif rec.status == AssignmentStatus.RUNNING:
                validate_assignment_transition(
                    rec.status, AssignmentStatus.SUSPENDED)
                await self._repo.update_assignment(
                    rec.id, status=AssignmentStatus.SUSPENDED,
                    stopped_at=self._now(),
                    error="member leaving")
            elif rec.status == AssignmentStatus.SUSPENDED:
                await self._repo.update_assignment(rec.id, account=None)

    async def retire_member(self, account: str, reason: str) -> None:
        """State-level leave for the RECONCILER (§9): same moves as
        leave_group but WITHOUT the membership seams — the recovery
        path repairs drift, it does not re-drive collaborators."""
        async with self._lock:
            member = self._require_member(account)
            group_name = member.group_name
            await self._release_live_work(account)
            validate_member_transition(member.eligibility,
                                       MemberEligibility.RETIRED, leaving=True)
            await self._repo.delete_member(account)
            await self._repo.bump_generation(group_name)
            await self._reload()
            await self._record("MemberRetired", group_name,
                               f"recovery retired {account}: {reason}",
                               account=account)

    async def bump_group_generation(self, group_name: str) -> int:
        """Bump the group's generation (the stale-dispatch fence). The
        reconciler uses this to fence zombie-process dispatches."""
        async with self._lock:
            state = await self._repo.bump_generation(group_name)
            self._groups[group_name] = state
            return state.generation

    # ---------------------------------------------------------- group pause

    async def pause_group(self, group_name: str) -> FleetEvent:
        async with self._lock:
            state = self._require_group(group_name)
            if state.paused:
                raise ValueError(f"group {group_name!r} is already paused")
            self._groups[group_name] = await self._repo.set_group(
                group_name, paused=True)
            return await self._record("GroupPaused", group_name,
                                      "operator paused the group")

    async def resume_group(self, group_name: str) -> FleetEvent:
        async with self._lock:
            state = self._require_group(group_name)
            if not state.paused:
                raise ValueError(f"group {group_name!r} is not paused")
            self._groups[group_name] = await self._repo.set_group(
                group_name, paused=False)
            return await self._record("GroupResumed", group_name,
                                      "operator resumed the group")

    # ------------------------------------------------------------- members

    async def pause_member(self, account: str) -> FleetEvent:
        async with self._lock:
            member = self._require_member(account)
            validate_member_transition(member.eligibility,
                                       MemberEligibility.PAUSED)
            member.eligibility = MemberEligibility.PAUSED
            await self._repo.upsert_member(member)
            self._members[account] = member
            return await self._record("MemberPaused", member.group_name,
                                      f"operator paused {account}",
                                      account=account)

    async def resume_member(self, account: str) -> FleetEvent:
        """Resume returns the member to ELIGIBLE (deterministic target;
        WAITING re-derives from the strategy on the next evaluation)."""
        async with self._lock:
            member = self._require_member(account)
            validate_member_transition(member.eligibility,
                                       MemberEligibility.ELIGIBLE)
            member.eligibility = MemberEligibility.ELIGIBLE
            await self._repo.upsert_member(member)
            self._members[account] = member
            return await self._record("MemberResumed", member.group_name,
                                      f"operator resumed {account}",
                                      account=account)

    async def mark_eligibility(self, account: str, target: str) -> FleetEvent:
        """Derived-state moves (§2 connectivity, observation flow): set
        the member's eligibility THROUGH the Phase A tables — an
        invalid move (e.g. PAUSED -> WAITING while an operator pause
        stands) raises InvalidTransition and changes nothing."""
        async with self._lock:
            member = self._require_member(account)
            validate_member_transition(member.eligibility, target)
            member.eligibility = target
            await self._repo.upsert_member(member)
            self._members[account] = member
            kind = "MemberWaiting" if target == MemberEligibility.WAITING \
                else "MemberEligibilityChanged"
            return await self._record(kind, member.group_name,
                                      f"{account} -> {target}",
                                      account=account)

    # ---------------------------------------------------------- assignments

    async def submit_task(self, group_name: str,
                          definition: TaskDefinition) -> FleetAssignmentRecord:
        """Register a task definition as a QUEUED assignment. The
        definition itself is plugin-declared at boot (FleetTaskHandler,
        rollout C+) — the service persists the assignment, not the
        plugin's declaration."""
        async with self._lock:
            if not isinstance(definition, TaskDefinition):
                raise ValueError(
                    "definition must be a TaskDefinition, got "
                    f"{type(definition).__name__}")
            state = self._require_group(group_name)
            rec = FleetAssignmentRecord(
                id=self._new_id(), group_name=group_name,
                task_id=definition.id, status=AssignmentStatus.QUEUED,
                priority=0, created_at=self._now(),
                resume_policy=definition.resume_policy,
                generation=state.generation)
            await self._repo.insert_assignment(rec)
            self._assignments[rec.id] = rec
            await self._record("TaskSubmitted", group_name,
                               f"task {definition.id} queued",
                               task_id=definition.id, assignment_id=rec.id)
            return rec

    def _validate_assignability(self, group_name: str, account: str) -> None:
        state = self._require_group(group_name)
        if state.paused:
            raise ValueError(
                f"group {group_name!r} is paused — no new work may start")
        member = self._require_member(account)
        if member.group_name != group_name:
            raise ValueError(
                f"account {account!r} manages with {member.group_name!r},"
                f" not {group_name!r}")
        if member.eligibility != MemberEligibility.ELIGIBLE:
            raise ValueError(
                f"account {account!r} is {member.eligibility}, not eligible")

    async def assign_task(self, assignment_id: str,
                          account: str) -> FleetAssignmentRecord:
        """QUEUED -> ASSIGNED, stamped with the live group generation
        (the stale-dispatch fence, spec §5)."""
        async with self._lock:
            rec = self._require_assignment(assignment_id)
            self._validate_assignability(rec.group_name, account)
            validate_assignment_transition(rec.status,
                                           AssignmentStatus.ASSIGNED)
            generation = self._groups[rec.group_name].generation
            await self._repo.update_assignment(
                rec.id, status=AssignmentStatus.ASSIGNED, account=account,
                generation=generation, error=None)
            rec = await self._repo.assignment(rec.id)
            self._assignments[rec.id] = rec
            await self._record("TaskAssigned", rec.group_name,
                               f"task {rec.task_id} assigned to {account}",
                               task_id=rec.task_id, assignment_id=rec.id,
                               account=account)
            return rec

    async def withdraw_task(self, assignment_id: str, reason: str = "operator withdraw"
                            ) -> FleetAssignmentRecord:
        """ASSIGNED -> QUEUED: the account binding releases, the task
        stays in the group's queue."""
        async with self._lock:
            rec = self._require_assignment(assignment_id)
            validate_assignment_transition(rec.status, AssignmentStatus.QUEUED)
            await self._repo.update_assignment(
                rec.id, status=AssignmentStatus.QUEUED, account=None)
            rec = await self._repo.assignment(rec.id)
            self._assignments[rec.id] = rec
            await self._record("TaskWithdrawn", rec.group_name, reason,
                               task_id=rec.task_id, assignment_id=rec.id,
                               account=rec.account)
            return rec

    async def _transition_assignment(
            self, assignment_id: str, new_status: str, kind: str,
            reason: str, *, retry_allowed: bool = False,
            extra_fields: Optional[dict] = None
    ) -> FleetAssignmentRecord:
        """The shared transition path: validate -> persist -> record."""
        async with self._lock:
            rec = self._require_assignment(assignment_id)
            validate_assignment_transition(rec.status, new_status,
                                           retry_allowed=retry_allowed)
            fields = {"status": new_status}
            if new_status == AssignmentStatus.RUNNING:
                fields["started_at"] = self._now()
            if new_status == AssignmentStatus.QUEUED:
                # queued work belongs to the GROUP, not an account: the
                # binding drops (relay requeue, fail-retry, resume all
                # return to the queue for the next assignment decision)
                fields["account"] = None
            if new_status in (AssignmentStatus.COMPLETED,
                              AssignmentStatus.FAILED,
                              AssignmentStatus.CANCELLED,
                              AssignmentStatus.SUSPENDED):
                fields["stopped_at"] = self._now()
            if extra_fields:
                fields.update(extra_fields)
            await self._repo.update_assignment(rec.id, **fields)
            rec = await self._repo.assignment(rec.id)
            self._assignments[rec.id] = rec
            await self._record(kind, rec.group_name, reason,
                               task_id=rec.task_id, assignment_id=rec.id,
                               account=rec.account)
            return rec

    async def report_started(self, assignment_id: str) -> FleetAssignmentRecord:
        return await self._transition_assignment(
            assignment_id, AssignmentStatus.RUNNING, "TaskStarted",
            "scheduler started the task")

    async def report_completed(self, assignment_id: str) -> FleetAssignmentRecord:
        return await self._transition_assignment(
            assignment_id, AssignmentStatus.COMPLETED, "TaskCompleted",
            "task finished")

    async def report_failed(self, assignment_id: str, error: str, *,
                            retry_allowed: bool = False
                            ) -> FleetAssignmentRecord:
        """RUNNING -> FAILED; under an explicit retry allowance the task
        re-queues immediately (the resume_policy decides the allowance —
        never a default, spec §2)."""
        rec = await self._transition_assignment(
            assignment_id, AssignmentStatus.FAILED, "TaskFailed",
            error or "task failed", extra_fields={"error": error})
        if retry_allowed:
            rec = await self._transition_assignment(
                assignment_id, AssignmentStatus.QUEUED, "TaskRequeued",
                f"retry policy allows requeue: {error}",
                retry_allowed=True)
        return rec

    async def suspend_task(self, assignment_id: str,
                           reason: str) -> FleetAssignmentRecord:
        return await self._transition_assignment(
            assignment_id, AssignmentStatus.SUSPENDED, "TaskSuspended",
            reason or "suspended")

    async def resume_task(self, assignment_id: str) -> FleetAssignmentRecord:
        """SUSPENDED -> QUEUED: prerequisites cleared; the assignment
        picks its next member through the strategy."""
        return await self._transition_assignment(
            assignment_id, AssignmentStatus.QUEUED, "TaskResumed",
            "prerequisites cleared")

    async def cancel_task(self, assignment_id: str,
                          reason: str = "operator cancel"
                          ) -> FleetAssignmentRecord:
        return await self._transition_assignment(
            assignment_id, AssignmentStatus.CANCELLED, "TaskCancelled",
            reason)

    async def handoff(self, assignment_id: str, to_account: str,
                      reason: str = "operator handoff"
                      ) -> FleetAssignmentRecord:
        """§5 handoff: validate the destination FIRST — a failed
        destination check never destroys the source assignment — then
        revoke the source, create the destination assignment (task
        identity and audit history survive), persist, notify. RUNNING
        sources need the drain machinery (rollout E); B hands off
        ASSIGNED work."""
        async with self._lock:
            source = self._require_assignment(assignment_id)
            if source.status != AssignmentStatus.ASSIGNED:
                raise ValueError(
                    "handoff requires an ASSIGNED task, got "
                    f"{source.status!r} (running work suspends first — "
                    "rollout E)")
            self._validate_assignability(source.group_name, to_account)
            destination = FleetAssignmentRecord(
                id=self._new_id(), group_name=source.group_name,
                task_id=source.task_id, status=AssignmentStatus.ASSIGNED,
                account=to_account, priority=source.priority,
                created_at=self._now(),
                resume_policy=source.resume_policy,
                generation=self._groups[source.group_name].generation)
            await self._repo.update_assignment(
                source.id, status=AssignmentStatus.CANCELLED,
                stopped_at=self._now(),
                error=f"handoff to {to_account}")
            await self._repo.insert_assignment(destination)
            self._assignments[destination.id] = destination
            self._assignments[source.id] = await self._repo.assignment(source.id)
            await self._record(
                "AssignmentHandoff", source.group_name,
                f"{reason}: {source.account} -> {to_account}",
                task_id=source.task_id, assignment_id=destination.id,
                account=to_account)
            return destination

    # ------------------------------------------------------------ cooldowns

    async def apply_cooldown(self, scope_type: str, scope_id: str,
                             reason: str, duration_s: float, *,
                             source: str = CooldownSource.MANUAL
                             ) -> FleetCooldownRecord:
        """Register a scoped cooldown (manual by default; observed/server
        arrive through the observation path in rollout D). Validation
        rides the Phase A record — scope, source, reason, expiry all
        checked there."""
        async with self._lock:
            now = self._now()
            try:
                duration_s = float(duration_s)
            except (TypeError, ValueError):
                raise ValueError(
                    f"duration_s must be a number, got {duration_s!r}")
            if duration_s <= 0:
                raise ValueError(
                    f"duration_s must be positive, got {duration_s!r}")
            CooldownRecord(scope_type=scope_type, scope_id=scope_id,
                           reason=reason, started_at=now,
                           expires_at=now + duration_s, source=source)
            row_id = await self._repo.insert_cooldown(
                scope_type, scope_id, reason, now, now + duration_s, source)
            await self._record(
                "CooldownStarted", self._group_for_scope(scope_type, scope_id),
                f"{scope_type} cooldown: {reason}")
            return await self._repo.cooldown(row_id)

    async def clear_cooldown(self, cooldown_id: int) -> FleetCooldownRecord:
        """Clear a MANUAL cooldown only (§6): observed/server windows are
        the game's own truth and ride to expiry."""
        async with self._lock:
            rec = await self._repo.cooldown(cooldown_id)
            if rec is None:
                raise ValueError(f"unknown cooldown {cooldown_id!r}")
            if rec.cleared_at is not None:
                raise ValueError(f"cooldown {cooldown_id} is already cleared")
            if rec.source != CooldownSource.MANUAL:
                raise ValueError(
                    f"cooldown {cooldown_id} is source={rec.source!r} — "
                    "only manual cooldowns clear")
            await self._repo.clear_cooldown(cooldown_id, self._now())
            await self._record(
                "CooldownEnded",
                self._group_for_scope(rec.scope_type, rec.scope_id),
                f"manual cooldown cleared: {rec.reason}")
            return await self._repo.cooldown(cooldown_id)

    async def cooldowns(self, *, scope_type: Optional[str] = None,
                        scope_id: Optional[str] = None,
                        active_only: bool = False
                        ) -> List[FleetCooldownRecord]:
        """Hub-facing read: DB-shaped rows (id, cleared_at — what the
        Fleet Hub displays)."""
        return await self._repo.cooldowns(
            scope_type=scope_type, scope_id=scope_id,
            active_only=active_only, now=self._now())

    async def policy_cooldowns(self, *, scope_type: Optional[str] = None,
                               scope_id: Optional[str] = None
                               ) -> List[CooldownRecord]:
        """Policy-facing read (§4/§6): uncleared rows as core
        CooldownRecords — the shape ``effective_cooldown`` consumes.
        Cleared rows are history, not policy input."""
        rows = await self._repo.cooldowns(
            scope_type=scope_type, scope_id=scope_id,
            active_only=False, now=self._now())
        return [CooldownRecord(
                    id=r.id, scope_type=r.scope_type, scope_id=r.scope_id,
                    reason=r.reason, started_at=r.started_at,
                    expires_at=r.expires_at, source=r.source)
                for r in rows if r.cleared_at is None]

    # ------------------------------------------------------------- reads

    def now(self) -> float:
        """The service's clock (injected seam) — policy evaluation and
        tests share ONE time authority."""
        return self._now()

    def group_state(self, group_name: str) -> Optional[FleetGroupState]:
        return self._groups.get(group_name)

    def member(self, account: str) -> Optional[FleetMemberRecord]:
        return self._members.get(account)

    def members(self, group_name: Optional[str] = None
                ) -> List[FleetMemberRecord]:
        rows = list(self._members.values())
        if group_name is not None:
            rows = [m for m in rows if m.group_name == group_name]
        return sorted(rows, key=lambda m: m.account)

    def assignment(self, assignment_id: str) -> Optional[FleetAssignmentRecord]:
        return self._assignments.get(assignment_id)

    def assignments(self, *, group_name: Optional[str] = None,
                    status: Optional[str] = None,
                    account: Optional[str] = None
                    ) -> List[FleetAssignmentRecord]:
        rows = list(self._assignments.values())
        if group_name is not None:
            rows = [r for r in rows if r.group_name == group_name]
        if status is not None:
            rows = [r for r in rows if r.status == status]
        if account is not None:
            rows = [r for r in rows if r.account == account]
        return sorted(rows, key=lambda r: r.created_at)

    async def transitions(self, *, group_name: Optional[str] = None,
                          limit: int = 200):
        return await self._repo.transitions(group_name=group_name,
                                            limit=limit)
