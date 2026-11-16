"""Workspace orchestration domain models (W1; supersedes core/fleet/models.py).

PURE module: no I/O, no Discord, no database, no framework imports.
Every workspace concept that other layers (service W3, chain runtime
W4, reconciler) speak is defined HERE first, so transitions and
policy are testable with zero connections (the rollout-A discipline
carried from the fleet module).

Carry-over guarantee (WORKSPACE_DESIGN.md §16): the correctness
vocabulary — eligibility states, task states, cooldown records, the
generation fence — moves from core/fleet/models.py RENAMED, not
redesigned; fleet tests map onto these one-to-one at W5.

New W1 vocabulary: chain steps + runs, per-step routing, channel
validity, preset identity/versions/provenance, plugin EMITS/FOLLOWS
declarations, signal identity.

Snowflake transport rule (carried): Discord ids are STRINGS end to
end — forms, JSON, storage, logs. JS Numbers round 19-digit ids.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "POLICY_CHECK_ORDER",
    "ChainDefinition",
    "ChainRun",
    "ChainRunStatus",
    "ChannelBinding",
    "ChannelDescriptor",
    "ChannelValidity",
    "CooldownRecord",
    "CooldownScopeType",
    "CooldownSource",
    "ExecutionMode",
    "FieldClass",
    "FollowSpec",
    "InvalidDefinition",
    "MemberEligibility",
    "MetricHint",
    "PolicyDecision",
    "PresetScope",
    "Provenance",
    "RoutingPolicy",
    "SignalIdentity",
    "StepExecution",
    "StepStatus",
    "TaskDefinition",
    "TaskStatus",
    "WorkspaceEvent",
    "WorkspaceTask",
    "effective_cooldown",
    "validate_channel_scope",
    "validate_plugin_declarations",
]


class InvalidDefinition(ValueError):
    """A domain definition (or its parts) failed validation.

    Precise, human-readable message naming the field — the same
    typo-trap discipline as settings validation; never a bare
    'invalid'. (Carried from the fleet module verbatim.)
    """


# --------------------------------------------------------------- enums


class MemberEligibility:
    """Per-member workspace eligibility states (carried from fleet §2).

    ``ELIGIBLE -> WAITING -> (PAUSED | COOLING) -> back to ELIGIBLE/
    WAITING``, plus ``RETIRED`` on leave. Connectivity is DERIVED from
    account status and deliberately NOT an eligibility state.
    """

    ELIGIBLE = "eligible"
    WAITING = "waiting"
    PAUSED = "paused"
    COOLING = "cooling"
    RETIRED = "retired"

    ALL: frozenset[str] = frozenset({ELIGIBLE, WAITING, PAUSED, COOLING, RETIRED})
    LIVE: frozenset[str] = frozenset({ELIGIBLE, WAITING, PAUSED, COOLING})


class ExecutionMode:
    """Who dispatches this account's work (decision 4's membership
    semantics; carried from fleet D1 with the workspace rename).

    ``INDEPENDENT`` — the account's own flows run (the pre-workspace
    world). ``MANAGED`` — the workspace owns what/when; independent
    dispatch is blocked; leaving restores INDEPENDENT.
    """

    INDEPENDENT = "independent"
    MANAGED = "managed"

    ALL: frozenset[str] = frozenset({INDEPENDENT, MANAGED})


class TaskStatus:
    """Workspace task states (carried from fleet §2) — the work UNIT,
    not the scheduler action.

        QUEUED -> ASSIGNED -> RUNNING -> COMPLETED | SUSPENDED | FAILED | CANCELLED

    ``SUSPENDED`` returns to ``QUEUED`` when prerequisites clear;
    ``FAILED`` resumes only under an explicit retry allowance.
    ``COMPLETED``/``CANCELLED`` are terminal.
    """

    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    SUSPENDED = "suspended"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL: frozenset[str] = frozenset(
        {QUEUED, ASSIGNED, RUNNING, COMPLETED, SUSPENDED, FAILED, CANCELLED}
    )
    ALWAYS_TERMINAL: frozenset[str] = frozenset({COMPLETED, CANCELLED})


class StepStatus:
    """Chain-step states (WORKSPACE_DESIGN §10.3 — NEW).

        PENDING -> READY -> RUNNING -> DONE
                     ↘ SKIPPED (plugin absent | gate denies | no
                                 eligible member | channel unusable)
                     ↘ FAILED (attempts exhausted)

    SKIPPED and DONE are terminal. FAILED -> READY happens only under
    an explicit retry allowance (mirrors TaskStatus).
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    ALL: frozenset[str] = frozenset({PENDING, READY, RUNNING, DONE, FAILED, SKIPPED, CANCELLED})
    ALWAYS_TERMINAL: frozenset[str] = frozenset({DONE, SKIPPED, CANCELLED})


class ChainRunStatus:
    """Chain-run states (NEW — partial completion is first-class).

    A run records which steps completed/skipped/failed; ``PARTIAL`` is
    the terminal state when some steps did not complete but
    ``on_skip: continue`` carried the run to its end.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL: frozenset[str] = frozenset({RUNNING, COMPLETED, PARTIAL, FAILED, CANCELLED})
    ALWAYS_TERMINAL: frozenset[str] = frozenset({COMPLETED, PARTIAL, FAILED, CANCELLED})


class TransitionMode:
    """How a membership transition drains in-flight work (carried;
    only DRAIN is modeled — spec §12 defers SUSPEND)."""

    DRAIN = "drain"

    ALL: frozenset[str] = frozenset({DRAIN})


class FieldClass:
    """Editable-while-running field classes (carried, locked
    2026-10-23 option C): HOT picks up next dispatch; BOUNDARY bumps
    the generation; STRUCTURAL needs pause -> edit -> resume."""

    HOT = "hot"
    BOUNDARY = "boundary"
    STRUCTURAL = "structural"

    ALL: frozenset[str] = frozenset({HOT, BOUNDARY, STRUCTURAL})


class CooldownScopeType:
    """Cooldown scopes (renamed from fleet §6: group/account/task ->
    workspace/member/task per WORKSPACE_DESIGN §8)."""

    WORKSPACE = "workspace"
    MEMBER = "member"
    TASK = "task"

    ALL: frozenset[str] = frozenset({WORKSPACE, MEMBER, TASK})


class CooldownSource:
    """Where a cooldown came from (carried). Clearing a MANUAL
    cooldown never clears an OBSERVED/SERVER one."""

    MANUAL = "manual"
    OBSERVED = "observed"
    SERVER = "server"

    ALL: frozenset[str] = frozenset({MANUAL, OBSERVED, SERVER})


class ChannelValidity:
    """Channel registry validity states (WORKSPACE_DESIGN §5.2 — NEW).

    The registry records IDENTITY; validity records current usability
    from runtime observation. ``available`` is the only state a
    channel-bound operation may resolve against.
    """

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    INACCESSIBLE = "inaccessible"
    DELETED = "deleted"
    STALE = "stale"

    ALL: frozenset[str] = frozenset({UNKNOWN, AVAILABLE, INACCESSIBLE, DELETED, STALE})
    USABLE: frozenset[str] = frozenset({AVAILABLE})


class RoutingPolicy:
    """Per-step target routing (decision 13 — NEW).

    ``SPECIFIC_ACCOUNT`` — a configured member is the target.
    ``FIRST_ELIGIBLE`` — exactly one member by deterministic order
    (joined_at ASC, account name ASC tie-break), persisted before
    execution. Fan-out is deferred out of v1.
    """

    SPECIFIC_ACCOUNT = "specific_account"
    FIRST_ELIGIBLE = "first_eligible"

    ALL: frozenset[str] = frozenset({SPECIFIC_ACCOUNT, FIRST_ELIGIBLE})


class PresetScope:
    """Preset template scopes (decision 8/15 — NEW): global baselines,
    workspace conventions, account-owned. All plugin-bound."""

    GLOBAL = "global"
    WORKSPACE = "workspace"
    ACCOUNT = "account"

    ALL: frozenset[str] = frozenset({GLOBAL, WORKSPACE, ACCOUNT})


#: Policy check order (WORKSPACE_DESIGN §12) — the policy engine's
#: contract. Fleet's six checks carried; check 7 is the new gate.
POLICY_CHECK_ORDER: tuple[str, ...] = (
    "framework_safety",
    "workspace_enabled",
    "member_eligible",
    "cooldowns_permit",
    "task_valid",
    "prerequisites",
    "plugin_and_channel",
)


# ------------------------------------------------------------- cooldowns


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidDefinition(f"{field_name} must be a non-empty string, got {value!r}")
    return value


@dataclass(frozen=True)
class CooldownRecord:
    """One scoped cooldown (carried) — a POLICY-side record.

    Distinct from the plugin-facing CooldownService rows. ids are
    STRINGS (snowflake transport rule); ``expires_at`` is an ABSOLUTE
    epoch timestamp (restart-safe).
    """

    scope_type: str  # CooldownScopeType value
    scope_id: str  # workspace name / account name / task id
    reason: str
    started_at: float
    expires_at: float
    source: str = CooldownSource.MANUAL  # CooldownSource value
    #: row identity when this record came from persistence (None for
    #: in-memory/validated-only instances)
    id: int | None = None

    def __post_init__(self) -> None:
        if self.scope_type not in CooldownScopeType.ALL:
            raise InvalidDefinition(
                f"cooldown scope_type must be one of "
                f"{sorted(CooldownScopeType.ALL)}, got {self.scope_type!r}"
            )
        if self.source not in CooldownSource.ALL:
            raise InvalidDefinition(
                f"cooldown source must be one of {sorted(CooldownSource.ALL)}, got {self.source!r}"
            )
        _require_str(self.scope_id, "cooldown scope_id")
        _require_str(self.reason, "cooldown reason")
        if self.expires_at < self.started_at:
            raise InvalidDefinition(
                f"cooldown expires_at ({self.expires_at}) precedes started_at ({self.started_at})"
            )

    def is_active(self, now: float | None = None) -> bool:
        """True while the cooldown's window still applies."""
        return (now if now is not None else time.time()) < self.expires_at

    @property
    def operator_clearable(self) -> bool:
        """Only MANUAL cooldowns clear on operator action."""
        return self.source == CooldownSource.MANUAL


def effective_cooldown(
    records: Iterable[CooldownRecord] | None, now: float | None = None
) -> CooldownRecord | None:
    """Most-restrictive-wins across cooldown records (carried).

    Returns the ACTIVE record with the LATEST expiry, or None when
    nothing is active. Never raises on an empty/None list.
    """
    moment = now if now is not None else time.time()
    active = [r for r in (records or ()) if r.is_active(moment)]
    if not active:
        return None
    return max(active, key=lambda r: r.expires_at)


# ------------------------------------------------------ task definitions


def validate_channel_scope(channel_ids: Iterable[str] | str) -> tuple[str, ...]:
    """Validate a task's channel scope (carried): non-empty positive
    set of channel-id STRINGS, no duplicates; returns the tuple."""

    if isinstance(channel_ids, str) or not isinstance(channel_ids, (list, tuple)):
        raise InvalidDefinition(
            f"channel_scope must be a list of channel-id strings, got {type(channel_ids).__name__}"
        )
    if not channel_ids:
        raise InvalidDefinition("channel_scope must name at least one channel")
    seen = set()
    for cid in channel_ids:
        if not isinstance(cid, str) or not cid.strip():
            raise InvalidDefinition(
                f"channel id must be a non-empty string, got {cid!r} "
                "(snowflakes are STRINGS end to end)"
            )
        if not cid.isdigit():
            raise InvalidDefinition(f"channel id must be a digit string, got {cid!r}")
        if cid in seen:
            raise InvalidDefinition(f"duplicate channel id {cid!r}")
        seen.add(cid)
    return tuple(channel_ids)


@dataclass(frozen=True)
class MetricHint:
    """Opt-in rollup declaration (carried): declared keys may roll up
    workspace-wide with the named aggregate; no schema guessing."""

    key: str
    label: str
    aggregate: str = "last"  # "sum" | "last"

    def __post_init__(self) -> None:
        _require_str(self.key, "metric hint key")
        _require_str(self.label, "metric hint label")
        if self.aggregate not in ("sum", "last"):
            raise InvalidDefinition(
                f"metric hint aggregate must be 'sum' or 'last', got {self.aggregate!r}"
            )


@dataclass(frozen=True)
class TaskDefinition:
    """A plugin-declared workspace task (carried from fleet §5/D5).

    ``channel_scope``  the channel resources the task works on
    ``data_key``       -> per-channel plugin data as task config
    ``cooldown_key``   which data key feeds the task's pacing
    ``resume_policy``  opaque-to-core policy name
    ``max_attempts``   total dispatch attempts allowed (>= 1)
    """

    id: str
    plugin: str
    channel_scope: tuple[str, ...]
    data_key: str = ""
    cooldown_key: str = ""
    resume_policy: str = ""
    max_attempts: int = 1
    metric_hints: tuple[MetricHint, ...] = ()
    field_classes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_str(self.id, "task definition id")
        _require_str(self.plugin, "task definition plugin")
        object.__setattr__(self, "channel_scope", validate_channel_scope(self.channel_scope))
        if self.max_attempts < 1:
            raise InvalidDefinition(f"max_attempts must be >= 1, got {self.max_attempts}")
        for name in ("data_key", "cooldown_key", "resume_policy"):
            value = getattr(self, name)
            if value != "" and (not isinstance(value, str) or not value.strip()):
                raise InvalidDefinition(f"{name} must be a non-empty string or '', got {value!r}")
        for hint in self.metric_hints:
            if not isinstance(hint, MetricHint):
                raise InvalidDefinition(f"metric_hints entries must be MetricHint, got {hint!r}")
        for knob, cls in self.field_classes.items():
            _require_str(knob, "field_classes knob name")
            if cls not in FieldClass.ALL:
                raise InvalidDefinition(
                    f"field class for {knob!r} must be one of {sorted(FieldClass.ALL)}, got {cls!r}"
                )

    def field_class_of(self, knob: str) -> str:
        """The knob's edit class — HOT when undeclared (always safe)."""
        return self.field_classes.get(knob, FieldClass.HOT)


@dataclass
class WorkspaceTask:
    """One task owned by one workspace, dispatched to one member
    (carried from fleet's TaskAssignment; group->workspace rename).
    A dispatch is rejected when ``generation`` no longer matches the
    live workspace generation (split-dispatch fencing)."""

    id: str
    workspace: str
    task_id: str
    account: str | None = None  # NULL while queued (binding released)
    status: str = TaskStatus.QUEUED
    error: str | None = None
    priority: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    stopped_at: float | None = None
    resume_policy: str = ""
    version: int = 1
    generation: int = 1

    def __post_init__(self) -> None:
        for name in ("id", "workspace", "task_id"):
            _require_str(getattr(self, name), f"task {name}")
        # account is None while QUEUED (binding released) — required
        # once ASSIGNED, which the transition validators enforce
        if self.account is not None and not str(self.account).strip():
            raise InvalidDefinition(
                f"task account must be a non-empty string or None, got {self.account!r}"
            )
        if self.status not in TaskStatus.ALL:
            raise InvalidDefinition(
                f"task status must be one of {sorted(TaskStatus.ALL)}, got {self.status!r}"
            )
        if self.version < 1 or self.generation < 1:
            raise InvalidDefinition(
                f"version/generation must be >= 1, got "
                f"version={self.version} generation={self.generation}"
            )

    def matches_generation(self, workspace_generation: int) -> bool:
        """The stale-dispatch fence (carried): a task claimed under
        generation N is rejected when the live generation differs."""
        return self.generation == workspace_generation


# ---------------------------------------------------------- policy shape


@dataclass(frozen=True)
class PolicyDecision:
    """The policy verdict (carried): ``allowed`` False carries a human
    ``reason`` and optionally the earliest ``retry_at`` (epoch)."""

    allowed: bool
    reason: str
    retry_at: float | None = None

    def __post_init__(self) -> None:
        if self.allowed and self.retry_at is not None:
            raise InvalidDefinition("an allowed decision carries no retry_at")
        _require_str(self.reason, "policy decision reason")


# ------------------------------------------------------------- channels


@dataclass(frozen=True)
class ChannelDescriptor:
    """A plain channel identity record (NEW) — what crosses INTO
    policy/handler land. Never a library object (the adapter resolves
    mentions/ids to these; plugins and the policy speak only these)."""

    channel_id: str  # snowflake STRING (transport rule)
    name: str = ""
    guild_id: str = ""  # "" for DM channels
    validity: str = ChannelValidity.UNKNOWN  # ChannelValidity value

    def __post_init__(self) -> None:
        if not self.channel_id.isdigit():
            raise InvalidDefinition(f"channel_id must be a digit string, got {self.channel_id!r}")
        if self.guild_id and not self.guild_id.isdigit():
            raise InvalidDefinition(f"guild_id must be a digit string or '', got {self.guild_id!r}")
        if self.validity not in ChannelValidity.ALL:
            raise InvalidDefinition(
                f"channel validity must be one of {sorted(ChannelValidity.ALL)}, "
                f"got {self.validity!r}"
            )

    @property
    def usable(self) -> bool:
        """§5.2 pre-execution contract: only AVAILABLE resolves."""
        return self.validity == ChannelValidity.AVAILABLE


@dataclass(frozen=True)
class ChannelBinding:
    """One (account, plugin, channel) binding (NEW — §5.1): the
    channel a plugin uses for that account. Initialized by copy from
    the account's assignments at assign time, then independent."""

    account: str
    plugin: str
    channel_id: str
    bound_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        _require_str(self.account, "binding account")
        _require_str(self.plugin, "binding plugin")
        if not self.channel_id.isdigit():
            raise InvalidDefinition(
                f"binding channel_id must be a digit string, got {self.channel_id!r}"
            )


# -------------------------------------------------------- declarations


@dataclass(frozen=True)
class FollowSpec:
    """One FOLLOW declaration on a plugin (decision 2 — NEW): the
    handler that runs when a matching trigger fires.

    ``signal``      a semantic trigger — ``<plugin>:<name>`` namespaced
    ``task_action`` a mechanical trigger — the scheduler action of the
                    terminal event ("message", "dm", "reaction", ...)
    Exactly one of the two is set per spec.
    """

    handler: str
    signal: str = ""
    task_action: str = ""

    def __post_init__(self) -> None:
        _require_str(self.handler, "follow handler")
        if bool(self.signal) == bool(self.task_action):
            raise InvalidDefinition(
                "FollowSpec sets exactly one of signal= or task_action="
                f" (got signal={self.signal!r}, task_action={self.task_action!r})"
            )


def validate_plugin_declarations(plugin_cls: Any) -> list[str]:
    """Load-time validation of a plugin's EMITS/FOLLOWS declarations
    (NEW). Returns a list of human-readable problems ([] = clean).

    Hook-name existence is an ERROR (a follow that can never run is a
    bug); unknown signal namespaces are the CALLER's choice to warn —
    this function reports them as problems and the loader decides.
    """

    problems: list[str] = []
    emits = getattr(plugin_cls, "EMITS", ())
    if not isinstance(emits, (tuple, list)):
        problems.append(
            f"{plugin_cls!r}: EMITS must be a tuple/list of names, got {type(emits).__name__}"
        )
    else:
        for name in emits:
            if not isinstance(name, str) or not name.strip() or ":" in name:
                problems.append(
                    f"{plugin_cls!r}: EMITS names are bare (no ':' namespacing — "
                    f"added at consumption), got {name!r}"
                )
    follows = getattr(plugin_cls, "FOLLOWS", ())
    if not isinstance(follows, (tuple, list)):
        problems.append(
            f"{plugin_cls!r}: FOLLOWS must be a tuple/list of FollowSpec, got {type(follows).__name__}"
        )
    else:
        for spec in follows:
            if not isinstance(spec, FollowSpec):
                problems.append(f"{plugin_cls!r}: FOLLOWS entries must be FollowSpec, got {spec!r}")
                continue
            handler = getattr(plugin_cls, spec.handler, None)
            if handler is None or not callable(handler):
                problems.append(
                    f"{plugin_cls!r}: FOLLOWS handler {spec.handler!r} is not a callable on the class"
                )
    return problems


# -------------------------------------------------------------- signals


@dataclass(frozen=True)
class SignalIdentity:
    """The durable identity of one emitted signal (decision 12 — NEW).

    The W2/W4 outbox persists exactly this shape BEFORE the emission
    is acknowledged; delivery state and retries hang off ``signal_id``
    and consumers deduplicate by it.
    """

    signal_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    account: str = ""
    plugin: str = ""
    name: str = ""  # bare name; consumed as "<plugin>:<name>"
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""  # originating task or chain run, when known
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        _require_str(self.account, "signal account")
        _require_str(self.plugin, "signal plugin")
        _require_str(self.name, "signal name")
        if ":" in self.name:
            raise InvalidDefinition(
                f"signal names are bare (namespacing is added at consumption), got {self.name!r}"
            )
        if not isinstance(self.payload, dict):
            raise InvalidDefinition(
                f"signal payload must be a dict, got {type(self.payload).__name__}"
            )

    @property
    def namespaced(self) -> str:
        """The consumption form — plugins cannot spoof another
        plugin's signal namespace."""
        return f"{self.plugin}:{self.name}"


# ---------------------------------------------------------------- chains


@dataclass(frozen=True)
class ChainStep:
    """One step in a chain definition (NEW — §10.2).

    ``routing``   RoutingPolicy.SPECIFIC_ACCOUNT (with ``target``)
                  or FIRST_ELIGIBLE (deterministic pick, persisted
                  before execution at the run level)
    ``on``        the trigger: {"signal": "<plugin>:<name>"} or
                  {"plugin": "<from-plugin>", "task_action": "<action>",
                   "event": "task_completed" | "task_failed"}
    ``gate``      optional per-step settings condition:
                  {"setting": name, "equals": value}
    ``emit``      bare signal name the step emits on completion
    """

    position: int
    plugin: str
    routing: str = RoutingPolicy.FIRST_ELIGIBLE
    target: str = ""  # required when routing == SPECIFIC_ACCOUNT
    on: dict[str, str] = field(default_factory=dict)
    gate: dict[str, Any] = field(default_factory=dict)
    emit: str = ""

    def __post_init__(self) -> None:
        if self.position < 1:
            raise InvalidDefinition(f"step position must be >= 1, got {self.position}")
        _require_str(self.plugin, "step plugin")
        if self.routing not in RoutingPolicy.ALL:
            raise InvalidDefinition(
                f"step routing must be one of {sorted(RoutingPolicy.ALL)}, got {self.routing!r}"
            )
        if self.routing == RoutingPolicy.SPECIFIC_ACCOUNT and not self.target.strip():
            raise InvalidDefinition("specific_account routing requires a target member name")
        if self.emit and ":" in self.emit:
            raise InvalidDefinition(
                f"step emit is a bare name (namespacing added at emission), got {self.emit!r}"
            )
        if self.on and not (set(self.on) <= {"signal", "task_action", "event", "plugin"}):
            raise InvalidDefinition(
                f"step 'on' keys must be signal/task_action/event/plugin, got {sorted(self.on)}"
            )
        if bool(self.on.get("signal")) == bool(self.on.get("task_action")):
            raise InvalidDefinition("step 'on' sets exactly one of signal= or task_action=")
        # a mechanical trigger names its source plugin (the actor whose
        # terminal event fires the step) — without it the binding would
        # fire on ANY account's sends
        if self.on.get("task_action") and not self.on.get("plugin"):
            raise InvalidDefinition("a task_action trigger requires plugin= (the acting plugin)")


@dataclass(frozen=True)
class ChainDefinition:
    """A named, versioned chain (NEW — decisions 2 + 14).

    ``schema_version``  definition shape version (structural changes)
    ``revision``        monotonic; edits bump it; validation on save
    ``on_skip``         "continue" (default) or "abort"
    A run captures an immutable snapshot at creation — edits affect
    future runs only.
    """

    name: str
    steps: tuple[ChainStep, ...]
    schema_version: int = 1
    revision: int = 1
    on_skip: str = "continue"
    enabled: bool = True

    def __post_init__(self) -> None:
        _require_str(self.name, "chain name")
        if not self.steps:
            raise InvalidDefinition(f"chain {self.name!r} must have at least one step")
        positions = [s.position for s in self.steps]
        if positions != sorted(positions) or len(set(positions)) != len(positions):
            raise InvalidDefinition(
                f"chain {self.name!r} step positions must be unique and ascending"
            )
        if self.on_skip not in ("continue", "abort"):
            raise InvalidDefinition(
                f"chain on_skip must be 'continue' or 'abort', got {self.on_skip!r}"
            )
        if self.schema_version < 1 or self.revision < 1:
            raise InvalidDefinition(
                f"schema_version/revision must be >= 1, got "
                f"schema_version={self.schema_version} revision={self.revision}"
            )


@dataclass
class ChainRun:
    """One execution of a chain against an immutable snapshot
    (decision 14 — NEW). ``definition`` is the captured snapshot; the
    live chain may have moved on."""

    id: str
    chain_id: str
    workspace: str
    definition: ChainDefinition
    status: str = ChainRunStatus.RUNNING
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    triggered_by: str = ""  # signal_id / task id / operator

    def __post_init__(self) -> None:
        for name in ("id", "chain_id", "workspace"):
            _require_str(getattr(self, name), f"chain run {name}")
        if not isinstance(self.definition, ChainDefinition):
            raise InvalidDefinition("chain run definition must be a ChainDefinition snapshot")
        if self.status not in ChainRunStatus.ALL:
            raise InvalidDefinition(
                f"chain run status must be one of {sorted(ChainRunStatus.ALL)}, got {self.status!r}"
            )


@dataclass
class StepExecution:
    """Durable execution identity for one step attempt (§10.4 — NEW):
    attempt count, target account, outcome, classified error."""

    run_id: str
    position: int
    attempt: int = 1
    target: str = ""
    outcome: str = ""  # StepStatus value set at terminal; "" while live
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def __post_init__(self) -> None:
        _require_str(self.run_id, "step execution run_id")
        if self.position < 1:
            raise InvalidDefinition(f"step execution position must be >= 1, got {self.position}")
        if self.attempt < 1:
            raise InvalidDefinition(f"attempt must be >= 1, got {self.attempt}")
        if self.outcome and self.outcome not in StepStatus.ALL:
            raise InvalidDefinition(
                f"step execution outcome must be a StepStatus, got {self.outcome!r}"
            )


# -------------------------------------------------------------- presets


@dataclass(frozen=True)
class Provenance:
    """Where an account-plugin configuration came from (decision 9 —
    NEW). Single source per pair in v1 (decision at D.4)."""

    source_preset_id: str
    source_version: int
    applied_at: float

    def __post_init__(self) -> None:
        _require_str(self.source_preset_id, "provenance source_preset_id")
        if self.source_version < 1:
            raise InvalidDefinition(
                f"provenance source_version must be >= 1, got {self.source_version}"
            )


# ---------------------------------------------------------------- events


@dataclass(frozen=True)
class WorkspaceEvent:
    """Envelope every authoritative state change carries (carried from
    fleet §3): persisted FIRST (write-ahead); bus delivery is an
    optimization. ``task``/``run``/``account`` ride along when relevant."""

    kind: str  # e.g. "TaskAssigned", "CooldownStarted", "ChainRunStarted"
    workspace: str
    generation: int
    reason: str
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurred_at: float = field(default_factory=time.time)
    task_id: str | None = None
    run_id: str | None = None
    account: str | None = None

    def __post_init__(self) -> None:
        _require_str(self.kind, "event kind")
        _require_str(self.workspace, "event workspace")
        _require_str(self.reason, "event reason")
        if self.generation < 1:
            raise InvalidDefinition(f"event generation must be >= 1, got {self.generation}")
