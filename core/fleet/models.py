"""Fleet orchestration domain models (PHASE 9, spec §2/§5/§6, rollouts A).

PURE module: no I/O, no Discord, no database, no framework imports.
Every fleet concept that other layers (service, policy engine, hub UI,
reconciler) speak is defined HERE first, so transitions are testable
with zero connections (rollout A's acceptance criterion).

Vocabulary boundaries (the architecture rule, spec §1):
  * the SCHEDULER's ``core.task_scheduler.Task`` is ONE outbound action;
  * the FLEET's ``TaskAssignment`` below is the long-running work unit
    that DISPATCHES those actions — different entity, different table.

Snowflake transport rule (live-found, Phase C): Discord ids are STRINGS
end to end — forms, JSON, storage, logs. JS Numbers round 19-digit ids.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional, Tuple

__all__ = [
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
]


class InvalidDefinition(ValueError):
    """A TaskDefinition (or its parts) failed validation.

    Precise, human-readable message naming the field — the same typo-trap
    discipline as settings validation; never a bare 'invalid'.
    """


# --------------------------------------------------------------- enums

class MemberEligibility:
    """Per-member fleet eligibility states (spec §2).

    ``ELIGIBLE -> WAITING -> (PAUSED | COOLING) -> back to ELIGIBLE/
    WAITING``, plus ``RETIRED`` on leave. Connectivity is DERIVED from
    account status and deliberately NOT an eligibility state. "Spent"
    is not either: a detected limit registers a scoped COOLDOWN and the
    strategy reassigns (spec §2).
    """

    ELIGIBLE = "eligible"
    WAITING = "waiting"
    PAUSED = "paused"
    COOLING = "cooling"
    RETIRED = "retired"

    ALL: FrozenSet[str] = frozenset({ELIGIBLE, WAITING, PAUSED, COOLING, RETIRED})
    LIVE: FrozenSet[str] = frozenset({ELIGIBLE, WAITING, PAUSED, COOLING})


class ExecutionMode:
    """D1 (locked): who dispatches this account's work.

    ``INDEPENDENT`` — the account's own flows run (the pre-fleet world).
    ``GROUP_MANAGED`` — the hub owns what/when; independent dispatch is
    blocked; leaving restores INDEPENDENT. The limiter and account
    config stay independent of membership either way.
    """

    INDEPENDENT = "independent"
    GROUP_MANAGED = "group_managed"

    ALL: FrozenSet[str] = frozenset({INDEPENDENT, GROUP_MANAGED})


class AssignmentStatus:
    """Fleet task states (spec §2) — the ASSIGNMENT, not the action.

        QUEUED -> ASSIGNED -> RUNNING -> COMPLETED | SUSPENDED | FAILED | CANCELLED

    ``SUSPENDED`` returns to ``QUEUED`` when prerequisites clear;
    ``FAILED`` resumes (-> QUEUED) only when the retry policy explicitly
    allows. ``COMPLETED``/``CANCELLED`` are terminal; ``FAILED`` is
    terminal unless a retry is allowed at transition time.
    """

    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    SUSPENDED = "suspended"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL: FrozenSet[str] = frozenset({
        QUEUED, ASSIGNED, RUNNING, COMPLETED, SUSPENDED, FAILED, CANCELLED})
    ALWAYS_TERMINAL: FrozenSet[str] = frozenset({COMPLETED, CANCELLED})


class TransitionMode:
    """D4 (locked): how an ownership transition drains in-flight work.

    ``DRAIN`` is the default (stop accepting new work; in-flight paced
    sends finish — they are <2s today). A ``SUSPEND`` mode is DEFERRED
    by design (spec §12) until long-lived loops exist and is therefore
    not modeled.
    """

    DRAIN = "drain"

    ALL: FrozenSet[str] = frozenset({DRAIN})


class AssignmentStrategy:
    """D3 (locked): the strategy slot for (re)assignment.

    ``MANUAL`` (default) — the operator assigns; ``RELAY`` — on a limit
    observation: suspend the owner's task, register a scoped cooldown,
    hand the task to the next eligible member. New strategies plug in
    later without schema change.
    """

    MANUAL = "manual"
    RELAY = "relay"

    ALL: FrozenSet[str] = frozenset({MANUAL, RELAY})


class FieldClass:
    """Editable-while-running field classes (locked 2026-10-23, option C).

    HOT        — next dispatch picks it up (always safe)
    BOUNDARY   — generation bump; in-flight completes, next dispatch
                 revalidates and adopts; parser listeners rebind
    STRUCTURAL — pause -> edit -> resume (rare)
    """

    HOT = "hot"
    BOUNDARY = "boundary"
    STRUCTURAL = "structural"

    ALL: FrozenSet[str] = frozenset({HOT, BOUNDARY, STRUCTURAL})


class CooldownScopeType:
    """Cooldown scopes (spec §6)."""

    GROUP = "group"
    ACCOUNT = "account"
    TASK = "task"

    ALL: FrozenSet[str] = frozenset({GROUP, ACCOUNT, TASK})


class CooldownSource:
    """Where a cooldown came from (spec §6).

    Clearing a MANUAL cooldown never clears an OBSERVED/SERVER one.
    """

    MANUAL = "manual"
    OBSERVED = "observed"
    SERVER = "server"

    ALL: FrozenSet[str] = frozenset({MANUAL, OBSERVED, SERVER})


#: Policy check order (spec §4) — the policy engine's contract, stated
#: here so the interface is visible before the engine exists (Phase C).
POLICY_CHECK_ORDER: Tuple[str, ...] = (
    "framework_safety",
    "group_enabled",
    "account_eligible",
    "cooldowns_permit",
    "task_valid",
    "prerequisites",
)


# ------------------------------------------------------------- cooldowns

def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidDefinition(
            f"{field_name} must be a non-empty string, got {value!r}")
    return value


@dataclass(frozen=True)
class CooldownRecord:
    """One scoped cooldown (spec §6) — a POLICY-side record.

    Distinct from the plugin-facing CooldownService rows: those are the
    plugin's own pacing data; this is the fleet's view of what blocks
    whom, evaluated most-restrictive-wins through the policy interface.

    ids are STRINGS (snowflake transport rule); ``expires_at`` is an
    ABSOLUTE epoch timestamp (restart-safe, same contract as the
    cooldown service).
    """

    scope_type: str                    # CooldownScopeType value
    scope_id: str                      # group id / account name / task id
    reason: str
    started_at: float
    expires_at: float
    source: str = CooldownSource.MANUAL   # CooldownSource value

    def __post_init__(self) -> None:
        if self.scope_type not in CooldownScopeType.ALL:
            raise InvalidDefinition(
                f"cooldown scope_type must be one of "
                f"{sorted(CooldownScopeType.ALL)}, got {self.scope_type!r}")
        if self.source not in CooldownSource.ALL:
            raise InvalidDefinition(
                f"cooldown source must be one of "
                f"{sorted(CooldownSource.ALL)}, got {self.source!r}")
        _require_str(self.scope_id, "cooldown scope_id")
        _require_str(self.reason, "cooldown reason")
        if self.expires_at < self.started_at:
            raise InvalidDefinition(
                f"cooldown expires_at ({self.expires_at}) precedes "
                f"started_at ({self.started_at})")

    def is_active(self, now: Optional[float] = None) -> bool:
        """True while the cooldown's window still applies."""
        return (now if now is not None else time.time()) < self.expires_at

    @property
    def operator_clearable(self) -> bool:
        """Only MANUAL cooldowns clear on operator action (spec §6)."""
        return self.source == CooldownSource.MANUAL


def effective_cooldown(records, now: Optional[float] = None):
    """Most-restrictive-wins across cooldown records (spec §6).

    Returns the ACTIVE record with the LATEST expiry (the one that
    blocks the longest), or None when nothing is active. Never raises
    on an empty/None list.
    """
    moment = now if now is not None else time.time()
    active = [r for r in (records or ()) if r.is_active(moment)]
    if not active:
        return None
    return max(active, key=lambda r: r.expires_at)


# ------------------------------------------------------ task definitions

def validate_channel_scope(channel_ids) -> Tuple[str, ...]:
    """Validate a task's channel scope (D5): non-empty positive set of
    channel-id STRINGS, no duplicates. Returns the normalized tuple.

    Digit enforcement is deliberate: the server parses digit-strings
    exactly (require_int); anything else is a transport bug upstream.
    """
    if isinstance(channel_ids, str) or not isinstance(channel_ids, (list, tuple)):
        raise InvalidDefinition(
            "channel_scope must be a list of channel-id strings, got "
            f"{type(channel_ids).__name__}")
    if not channel_ids:
        raise InvalidDefinition("channel_scope must name at least one channel")
    seen = set()
    for cid in channel_ids:
        if not isinstance(cid, str) or not cid.strip():
            raise InvalidDefinition(
                f"channel id must be a non-empty string, got {cid!r} "
                "(snowflakes are STRINGS end to end)")
        if not cid.isdigit():
            raise InvalidDefinition(
                f"channel id must be a digit string, got {cid!r}")
        if cid in seen:
            raise InvalidDefinition(f"duplicate channel id {cid!r}")
        seen.add(cid)
    return tuple(channel_ids)


@dataclass(frozen=True)
class MetricHint:
    """Opt-in rollup declaration for B3 data metrics (spec §7).

    Undeclared keys stay LOCAL (channel view only); declared keys may
    roll up fleet-wide with the named aggregate. No schema guessing.
    """

    key: str
    label: str
    aggregate: str = "last"        # "sum" | "last"

    def __post_init__(self) -> None:
        _require_str(self.key, "metric hint key")
        _require_str(self.label, "metric hint label")
        if self.aggregate not in ("sum", "last"):
            raise InvalidDefinition(
                f"metric hint aggregate must be 'sum' or 'last', "
                f"got {self.aggregate!r}")


@dataclass(frozen=True)
class TaskDefinition:
    """A plugin-declared fleet task (spec §5, D5) — opt-in capability.

    ``channel_scope``  the channel resources the task works on (positive
                       set of channel-id strings; B3 per-channel plugin
                       data is the task's config source)
    ``data_key``       -> B3 per-channel plugin data as task config
    ``cooldown_key``   which data key feeds the task's pacing; the
                       policy engine reads cooldowns through this mapping
    ``resume_policy``  opaque-to-core policy name; the strategy and the
                       retry guard interpret it
    ``max_attempts``   total dispatch attempts allowed (>= 1)
    ``field_classes``  editable-while-running classes per definition
                       knob (option C); structural edits pause->edit->
                       resume, boundary edits bump the generation
    """

    id: str
    plugin: str
    channel_scope: Tuple[str, ...]
    data_key: str = ""
    cooldown_key: str = ""
    resume_policy: str = ""
    max_attempts: int = 1
    metric_hints: Tuple[MetricHint, ...] = ()
    field_classes: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_str(self.id, "task definition id")
        _require_str(self.plugin, "task definition plugin")
        # channel_scope normalized through the validator (str/strip rules)
        object.__setattr__(self, "channel_scope",
                           validate_channel_scope(self.channel_scope))
        if self.max_attempts < 1:
            raise InvalidDefinition(
                f"max_attempts must be >= 1, got {self.max_attempts}")
        for name in ("data_key", "cooldown_key", "resume_policy"):
            value = getattr(self, name)
            if value != "" and (not isinstance(value, str) or not value.strip()):
                raise InvalidDefinition(
                    f"{name} must be a non-empty string or '', got {value!r}")
        for hint in self.metric_hints:
            if not isinstance(hint, MetricHint):
                raise InvalidDefinition(
                    f"metric_hints entries must be MetricHint, got {hint!r}")
        for knob, cls in self.field_classes.items():
            _require_str(knob, "field_classes knob name")
            if cls not in FieldClass.ALL:
                raise InvalidDefinition(
                    f"field class for {knob!r} must be one of "
                    f"{sorted(FieldClass.ALL)}, got {cls!r}")

    def field_class_of(self, knob: str) -> str:
        """The knob's edit class — HOT when undeclared (always safe)."""
        return self.field_classes.get(knob, FieldClass.HOT)


@dataclass
class TaskAssignment:
    """One task assigned to one account in one group (spec §5).

    Identity + audit history survive handoff; a dispatch is rejected
    when the assignment's ``generation`` no longer matches the live
    group generation (split-dispatch fencing).
    """

    id: str
    group_id: str
    task_id: str
    account_id: str
    status: str = AssignmentStatus.QUEUED
    priority: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    stopped_at: Optional[float] = None
    resume_policy: str = ""
    version: int = 1
    generation: int = 1

    def __post_init__(self) -> None:
        for name in ("id", "group_id", "task_id", "account_id"):
            _require_str(getattr(self, name), f"assignment {name}")
        if self.status not in AssignmentStatus.ALL:
            raise InvalidDefinition(
                f"assignment status must be one of "
                f"{sorted(AssignmentStatus.ALL)}, got {self.status!r}")
        if self.version < 1 or self.generation < 1:
            raise InvalidDefinition(
                f"version/generation must be >= 1, got "
                f"version={self.version} generation={self.generation}")

    def matches_generation(self, group_generation: int) -> bool:
        """The stale-dispatch fence (spec §5): an assignment made under
        generation N is rejected when the live generation differs."""
        return self.generation == group_generation


@dataclass(frozen=True)
class PolicyDecision:
    """The load-bearing policy interface's verdict (spec §4).

    ``allowed`` False carries a human ``reason`` and optionally the
    earliest ``retry_at`` (epoch) the same request may be re-evaluated.
    """

    allowed: bool
    reason: str
    retry_at: Optional[float] = None

    def __post_init__(self) -> None:
        if self.allowed and self.retry_at is not None:
            raise InvalidDefinition(
                "an allowed decision carries no retry_at")
        _require_str(self.reason, "policy decision reason")


# ---------------------------------------------------------------- events

def observation_dedup_key(source_account: str, message_id: str,
                          observation_type: str) -> Tuple[str, str, str]:
    """Dedup key for observations (spec §3): the SAME parsed marker
    delivered twice must register once. All parts STRINGS."""
    return (str(source_account), str(message_id), str(observation_type))


@dataclass(frozen=True)
class FleetEvent:
    """Envelope every authoritative state change carries (spec §3).

    Persisted FIRST (write-ahead); bus delivery is an optimization.
    ``task``/``assignment``/``account`` ride along when relevant.
    """

    kind: str                         # e.g. "TaskAssigned", "CooldownStarted"
    group_id: str
    generation: int
    reason: str
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurred_at: float = field(default_factory=time.time)
    task_id: Optional[str] = None
    assignment_id: Optional[str] = None
    account_id: Optional[str] = None

    def __post_init__(self) -> None:
        _require_str(self.kind, "event kind")
        _require_str(self.group_id, "event group_id")
        _require_str(self.reason, "event reason")
        if self.generation < 1:
            raise InvalidDefinition(
                f"event generation must be >= 1, got {self.generation}")
