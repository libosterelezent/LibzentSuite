"""WorkspacePolicy — the dispatch eligibility gate (W1; extends the
carried fleet check order).

    PolicyDecision evaluate(facts) -> {allowed, reason, retry_at}

PURE over plain state facts: the W3 service builds a
:class:`DispatchFacts` from its projection (and live seams), calls
:meth:`WorkspacePolicy.evaluate`, and the check order below is the
whole verdict. This keeps the load-bearing interface testable with
zero connections — the W1 discipline.

Check order (POLICY_CHECK_ORDER, WORKSPACE_DESIGN §12):
  1. framework safety (estop / scheduler halted / projection loaded)
  2. workspace enabled ∧ not paused (generation current)
  3. member eligible (connected ∧ eligibility permits)
  4. cooldowns permit (most-restrictive wins)
  5. task valid (held by the account, ASSIGNED, generation fence)
  6. prerequisites met (chain position ordering)
  7. plugin + channel: assigned ∧ enabled ∧ chain gate passes ∧ a
     usable channel binding resolves      <- the NEW check

Two inviolable boundaries (carried from the fleet engine):
  * the LIMITER stays the pacing floor — this engine decides WHETHER,
    never WHEN;
  * MEMBERSHIP decides reach: an account that is not a workspace
    member is NONE of this engine's business.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    CooldownRecord,
    MemberEligibility,
    PolicyDecision,
    TaskStatus,
    effective_cooldown,
)

__all__ = ["DispatchFacts", "WorkspacePolicy"]


@dataclass
class DispatchFacts:
    """The plain-state inputs to one dispatch evaluation.

    Everything the policy needs, bundled by the caller — no I/O, no
    service references, no locks. Facts that are ``None``/empty mean
    "not applicable" (e.g. no chain step context for a free task).
    """

    # 1. framework safety — False fails closed for everyone
    framework_safe: bool = True
    # 2. workspace state (None = the account is not a member)
    workspace_name: str | None = None
    workspace_enabled: bool = True
    workspace_paused: bool = False
    workspace_generation: int = 0
    # 3. member state
    member_eligibility: str = MemberEligibility.ELIGIBLE
    account_connected: bool = True
    # 4. cooldown records across the composed scopes (already
    #    gathered by the caller: workspace + member [+ task])
    cooldowns: tuple[CooldownRecord, ...] = ()
    #: the caller's clock reading for cooldown evaluation
    now: float = 0.0
    # 5. task validity (free tasks pass None-by-construction: a task
    #    row is only supplied when a dispatch is linked to one)
    task_status: str | None = None
    task_held_by: str | None = None
    task_generation: int | None = None
    # 6. prerequisites (chain position ordering — False blocks)
    prerequisites_met: bool = True
    # 7. plugin + channel (the NEW gate)
    plugin_assigned: bool = True
    plugin_enabled: bool = True
    gate_passed: bool = True
    channel_binding_usable: bool = True
    channel_binding_reason: str = ""
    #: identifying context for reasons (never validated here)
    account: str = ""
    task_label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "cooldowns", tuple(self.cooldowns or ()))


@dataclass(frozen=True)
class _WorkspaceFacts:
    """Internal normalized view (validation happens once)."""

    workspace_name: str | None
    enabled: bool
    paused: bool
    generation: int


class WorkspacePolicy:
    """May this account run this work now? Pure over :class:`DispatchFacts`.

    Stateless — one instance serves the whole process; the W3 service
    and the W4 chain runtime both call it with facts they gather.
    """

    def evaluate(self, facts: DispatchFacts) -> PolicyDecision:
        # 1. framework safety — fail closed, always first
        if not facts.framework_safe:
            return PolicyDecision(allowed=False, reason="framework safety engaged (estop/halt)")

        # membership decides reach (carried boundary): a non-member's
        # independent dispatch is untouched by this engine
        if facts.workspace_name is None:
            return PolicyDecision(allowed=True, reason="not workspace-managed")

        # 2. workspace enabled / not paused
        if facts.workspace_paused:
            return PolicyDecision(
                allowed=False, reason=f"workspace {facts.workspace_name!r} is paused"
            )
        if not facts.workspace_enabled:
            return PolicyDecision(
                allowed=False, reason=f"workspace {facts.workspace_name!r} is disabled"
            )

        # 3. member eligible (connectivity is a fact, eligibility the
        #    derived state — both carried from the fleet engine)
        if not facts.account_connected:
            return PolicyDecision(
                allowed=False, reason=f"account {facts.account!r} is not connected"
            )
        if facts.member_eligibility != MemberEligibility.ELIGIBLE:
            return PolicyDecision(
                allowed=False, reason=f"account {facts.account!r} is {facts.member_eligibility}"
            )

        # 4. cooldowns permit — most-restrictive wins (carried)
        worst = effective_cooldown(facts.cooldowns, now=facts.now)
        if worst is not None:
            return PolicyDecision(
                allowed=False,
                reason=f"cooldown ({worst.scope_type}:{worst.scope_id}) — {worst.reason}",
                retry_at=worst.expires_at,
            )

        # 5. task valid: linked dispatches validate the full chain —
        #    held-by, status, and the generation fence (carried)
        if facts.task_status is not None:
            if facts.task_held_by is not None and facts.task_held_by != facts.account:
                return PolicyDecision(
                    allowed=False,
                    reason=f"task {facts.task_label!r} is held by "
                    f"{facts.task_held_by!r}, not {facts.account!r}",
                )
            if facts.task_status != TaskStatus.ASSIGNED:
                return PolicyDecision(
                    allowed=False,
                    reason=f"task {facts.task_label!r} is {facts.task_status}",
                )
            if (
                facts.task_generation is not None
                and facts.task_generation != facts.workspace_generation
            ):
                return PolicyDecision(
                    allowed=False,
                    reason=f"task generation {facts.task_generation} != live"
                    f" {facts.workspace_generation} — stale dispatch fenced",
                )

        # 6. prerequisites (chain position ordering)
        if not facts.prerequisites_met:
            return PolicyDecision(
                allowed=False,
                reason=f"prerequisites for {facts.task_label!r} not met (chain ordering)",
            )

        # 7. plugin + channel — the workspace gate (NEW): the
        #    "if settings/plugins allows it" of the goal, plus the
        #    WHERE: a channel binding that cannot resolve is a
        #    visible skip at dispatch, never a blind send
        if not facts.plugin_assigned:
            return PolicyDecision(
                allowed=False,
                reason=f"plugin not assigned to {facts.account!r} (visible skip)",
            )
        if not facts.plugin_enabled:
            return PolicyDecision(
                allowed=False,
                reason=f"plugin disabled on {facts.account!r} (visible skip)",
            )
        if not facts.gate_passed:
            return PolicyDecision(
                allowed=False, reason="chain gate denies this step (visible skip)"
            )
        if not facts.channel_binding_usable:
            detail = f" — {facts.channel_binding_reason}" if facts.channel_binding_reason else ""
            return PolicyDecision(
                allowed=False,
                reason=f"no usable channel binding{detail} (visible skip)",
            )

        return PolicyDecision(allowed=True, reason="workspace checks passed")
