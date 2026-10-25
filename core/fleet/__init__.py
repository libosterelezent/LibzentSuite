"""Fleet orchestration package (PHASE 9 — rollout A: models + transitions).

One module per the spec's implementation shape (§1): internal components
in ``core/fleet/``, classes extracted only when testing justifies it.
This package is PURE at rollout A — the service (B), policy engine (C),
scheduler/limiter integration (C), parser observations (D), ownership
transitions (E), hub view (F) and recovery (G) build ON these models.

Design source: docs/design/fleet-orchestration-hub.md (decisions locked
2026-10-22; editable-while-running locked 2026-10-23).
"""

from .capability import FleetTaskHandler, definitions_of, handler_of
from .models import (
    POLICY_CHECK_ORDER,
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
    PolicyDecision,
    TaskAssignment,
    TaskDefinition,
    TransitionMode,
    effective_cooldown,
    observation_dedup_key,
    validate_channel_scope,
)
from .observations import (
    OBS_ACCOUNT_CONNECTED,
    OBS_ACCOUNT_DISCONNECTED,
    OBS_LIMIT_MARKER,
    OBS_SERVER_COOLDOWN,
    FleetObservations,
    Observation,
)
from .policy import FleetPolicy
from .reconciler import FleetReconciler
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
    "ASSIGNMENT_TRANSITIONS",
    # transitions
    "MEMBER_TRANSITIONS",
    "OBS_ACCOUNT_CONNECTED",
    "OBS_ACCOUNT_DISCONNECTED",
    "OBS_LIMIT_MARKER",
    "OBS_SERVER_COOLDOWN",
    "POLICY_CHECK_ORDER",
    "AssignmentStatus",
    "AssignmentStrategy",
    "CooldownRecord",
    "CooldownScopeType",
    "CooldownSource",
    "ExecutionMode",
    "FieldClass",
    "FleetEvent",
    # observations (rollout D)
    "FleetObservations",
    # policy
    "FleetPolicy",
    # reconciler (§9)
    "FleetReconciler",
    # service
    "FleetService",
    # capability (§5)
    "FleetTaskHandler",
    "InvalidDefinition",
    "InvalidTransition",
    # models
    "MemberEligibility",
    "MetricHint",
    "Observation",
    "PolicyDecision",
    "TaskAssignment",
    "TaskDefinition",
    "TransitionMode",
    "assignment_can_transition",
    "definitions_of",
    "effective_cooldown",
    "handler_of",
    "member_can_transition",
    "observation_dedup_key",
    "validate_assignment_transition",
    "validate_channel_scope",
    "validate_member_transition",
]
