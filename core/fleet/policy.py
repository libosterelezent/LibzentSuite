"""FleetPolicy — the GroupPolicyEngine (PHASE 9, rollout C, spec §4).

    PolicyDecision evaluate(account, work) -> {allowed, reason, retry_at}

THE load-bearing interface. Check order (POLICY_CHECK_ORDER, pinned in
Phase A): framework safety -> group enabled/not paused -> account
connected + eligible + not paused -> cooldowns permit (most-restrictive
wins) -> task assigned + valid (generation match) -> prerequisites.

Two inviolable boundaries (spec §4):
  * the LIMITER stays the pacing floor — this engine decides WHETHER,
    never WHEN; the scheduler still acquires a grant after every
    allowed verdict, and pacing always wins on the clock;
  * MEMBERSHIP decides the fleet's reach (D1): an account that is not
    a group-managed member is NONE of this engine's business — its
    independent dispatch flows untouched.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from .models import (
    AssignmentStatus,
    CooldownScopeType,
    MemberEligibility,
    PolicyDecision,
    effective_cooldown,
)

__all__ = ["FleetPolicy"]

#: park slice bounds for the scheduler bridge (seconds): short enough
#: that an operator unpause lands quickly, long enough to stay idle
GUARD_MIN_PARK_S = 0.05
GUARD_MAX_PARK_S = 5.0


class FleetPolicy:
    """May this account run this work now? Pure over service state.

    ``account_connected`` / ``group_enabled`` are async seams (default
    permissive): bootstrap wires the real readers; rollout D's
    observation flow moves connectivity into member eligibility (the
    derived state §2 prescribes) and tightens them.
    """

    def __init__(
        self,
        fleet: Any,
        *,
        account_connected: Optional[Callable[[str], Awaitable[bool]]] = None,
        group_enabled: Optional[Callable[[str], Awaitable[bool]]] = None,
    ) -> None:
        self._fleet = fleet
        self._connected = account_connected
        self._enabled = group_enabled

    # ------------------------------------------------------------- checks

    async def evaluate(self, account: str, *,
                       assignment_id: Optional[str] = None
                       ) -> PolicyDecision:
        """The §4 verdict for one account's dispatch. ``assignment_id``
        opts into the task-validity checks (generation fence, held-by-
        account); the scheduler-level guard evaluates account scope."""
        # 1. framework safety: the projection must be loaded — an
        #    unloaded fleet state is a boot bug, fail CLOSED for members
        #    (never dispatch on unread state), open for non-members.
        if not getattr(self._fleet, "_groups", {}):
            member_maybe = False
        else:
            member_maybe = self._fleet.member(account) is not None
        if not member_maybe:
            # not fleet-managed (or fleet empty): independent dispatch
            # is untouched (D1) — allowed, no fleet reason to give
            return PolicyDecision(
                allowed=True, reason="not fleet-managed")

        member = self._fleet.member(account)
        group_name = member.group_name
        state = self._fleet.group_state(group_name)

        # 2. group enabled / not paused
        if state is None:
            return PolicyDecision(
                allowed=False,
                reason=f"group {group_name!r} has no fleet state")
        if state.paused:
            return PolicyDecision(
                allowed=False, reason=f"group {group_name!r} is paused")
        if self._enabled is not None and not await self._enabled(group_name):
            return PolicyDecision(
                allowed=False, reason=f"group {group_name!r} is disabled")

        # 3. account connected + eligible + not paused
        if self._connected is not None and not await self._connected(account):
            return PolicyDecision(
                allowed=False, reason=f"account {account!r} is not connected")
        if member.eligibility != MemberEligibility.ELIGIBLE:
            return PolicyDecision(
                allowed=False,
                reason=f"account {account!r} is {member.eligibility}")

        # 4. cooldowns permit — most-restrictive across the composed
        #    scopes (group + account [+ the assignment's task])
        task_id: Optional[str] = None
        if assignment_id is not None:
            rec = self._fleet.assignment(assignment_id)
            task_id = rec.task_id if rec is not None else None
        scopes = [(CooldownScopeType.GROUP, group_name),
                  (CooldownScopeType.ACCOUNT, account)]
        if task_id is not None:
            scopes.append((CooldownScopeType.TASK, task_id))
        blocking = []
        now = self._fleet.now()
        for scope_type, scope_id in scopes:
            records = await self._fleet.policy_cooldowns(
                scope_type=scope_type, scope_id=scope_id)
            effective = effective_cooldown(records, now=now)
            if effective is not None:
                blocking.append(effective)
        worst = effective_cooldown(blocking, now=now)
        if worst is not None:
            return PolicyDecision(
                allowed=False,
                reason=f"cooldown ({worst.scope_type}:{worst.scope_id})"
                       f" — {worst.reason}",
                retry_at=worst.expires_at)

        # 5. task assigned + valid (only when an assignment is in hand)
        if assignment_id is not None:
            rec = self._fleet.assignment(assignment_id)
            if rec is None:
                return PolicyDecision(
                    allowed=False,
                    reason=f"unknown fleet assignment {assignment_id!r}")
            if rec.account != account:
                return PolicyDecision(
                    allowed=False,
                    reason=f"assignment {assignment_id!r} is held by "
                           f"{rec.account!r}, not {account!r}")
            if rec.status != AssignmentStatus.ASSIGNED:
                return PolicyDecision(
                    allowed=False,
                    reason=f"assignment {assignment_id!r} is {rec.status}")
            if rec.generation != state.generation:
                return PolicyDecision(
                    allowed=False,
                    reason=f"assignment generation {rec.generation} != live"
                           f" {state.generation} — stale dispatch fenced")

        # 6. prerequisites: suspend/resume own this at the service level
        #    (SUSPENDED never reaches dispatch); nothing further here.
        return PolicyDecision(allowed=True, reason="fleet checks passed")

    # ------------------------------------------------------ scheduler gate

    def scheduler_guard(self) -> Callable[[Any], Awaitable[PolicyDecision]]:
        """The bridge the TaskScheduler parks on (spec §4's pre-dispatch
        + post-grant revalidation). Duck-typed: the scheduler reads
        ``.allowed`` / ``.retry_at`` and imports nothing from core.fleet.
        """
        async def guard(task: Any) -> PolicyDecision:
            return await self.evaluate(str(getattr(task, "account", "")))
        return guard
