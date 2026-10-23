"""Fleet orchestration package (PHASE 9 — rollout A: models + transitions).

One module per the spec's implementation shape (§1): internal components
in ``core/fleet/``, classes extracted only when testing justifies it.
This package is PURE at rollout A — the service (B), policy engine (C),
scheduler/limiter integration (C), parser observations (D), ownership
transitions (E), hub view (F) and recovery (G) build ON these models.

Design source: docs/design/fleet-orchestration-hub.md (decisions locked
2026-10-22; editable-while-running locked 2026-10-23).
"""
from .models import (
    AssignmentStatus,
    AssignmentStrategy,
    CooldownRecord,
    CooldownScopeType,
    CooldownSource,
    ExecutionMode,
    FieldClass,
    FleetEvent,
    InvalidDefinition,
    MemberEligibility,
    MetricHint,
    POLICY_CHECK_ORDER,
    PolicyDecision,
    TaskAssignment,
    TaskDefinition,
    TransitionMode,
    effective_cooldown,
    observation_dedup_key,
    validate_channel_scope,
)
from .policy import FleetPolicy
from .service import FleetService
from .transitions import (
    ASSIGNMENT_TRANSITIONS,
    MEMBER_TRANSITIONS,
    InvalidTransition,
    assignment_can_transition,
    member_can_transition,
    validate_assignment_transition,
    validate_member_transition,
)

__all__ = [
    # models
    "MemberEligibility",
    "ExecutionMode",
    "AssignmentStatus",
    "TransitionMode",
    "AssignmentStrategy",
    "FieldClass",
    "CooldownScopeType",
    "CooldownSource",
    "CooldownRecord",
    "effective_cooldown",
    "MetricHint",
    "TaskDefinition",
    "TaskAssignment",
    "PolicyDecision",
    "FleetEvent",
    "observation_dedup_key",
    "POLICY_CHECK_ORDER",
    "validate_channel_scope",
    "InvalidDefinition",
    # service
    "FleetService",
    # policy
    "FleetPolicy",
    # transitions
    "MEMBER_TRANSITIONS",
    "ASSIGNMENT_TRANSITIONS",
    "InvalidTransition",
    "validate_member_transition",
    "member_can_transition",
    "validate_assignment_transition",
    "assignment_can_transition",
]
