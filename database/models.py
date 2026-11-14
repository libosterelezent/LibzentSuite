"""Database models: plain dataclasses crossing the persistence boundary.

These are the ONLY shapes the rest of the framework uses when talking about
persisted state. Repositories (database/repositories/*) construct/return them;
managers consume them. Keep this module dependency-free (no imports from
core.* beyond nothing) so database/ stays independently testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: policy levels (spec §5): most specific wins-by-composition (max interval)
POLICY_LEVELS = ("global", "account", "plugin", "account_plugin")


@dataclass
class AccountRecord:
    """PERSISTENT account data (spec §1: persistent vs runtime split).

    Runtime data (client, cache, latency, uptime...) lives in
    core.account_manager.AccountRuntime and is NEVER persisted here.
    """

    id: int
    name: str
    token_ref: str | None = None  # .env key; token VALUES never stored
    enabled: bool = True
    sync_group: str | None = None  # accounts that act in sync (optional)
    #: THE workspace membership pointer (decision 11) — authoritative;
    #: the v9 sync_group column stays read-only until v11 drops it
    workspace: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)  # framework layer
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "token_ref": self.token_ref,
            "enabled": self.enabled,
            "sync_group": self.sync_group,
            "workspace": self.workspace,
            "settings": dict(self.settings),
            "created_at": round(self.created_at, 3),
        }


@dataclass
class SyncGroupRecord:
    """A named sync group: accounts that act in sync (shared pacing).

    MEMBERSHIP is deliberately NOT stored here: an account belongs to at
    most one group, recorded on ``accounts.sync_group`` (the group NAME —
    the membership pointer), exactly like the pre-group-table field. This
    table is the group's first-class identity + metadata only.
    """

    id: int
    name: str
    description: str = ""
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
        }


@dataclass
class PluginProfileRecord:
    """A named per-plugin settings profile (migration v7).

    ``settings`` is the DECODED settings_json payload. Persistence never
    schema-validates it (the ControlAPI does, against the plugin's
    Setting schema); the reserved ``"channels"`` key (channel scope,
    see core/plugin_scopes.py) passes through like any other key.
    """

    id: int
    plugin: str
    name: str
    description: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plugin": self.plugin,
            "name": self.name,
            "description": self.description,
            "settings": dict(self.settings),
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
        }


@dataclass
class PluginMetadata:
    """A discovered plugin's manifest as persisted for the UI."""

    name: str
    version: str = "0.0.0"
    description: str = ""
    settings_manifest: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "settings": list(self.settings_manifest),
        }


@dataclass
class RatePolicyRecord:
    """One stored rate policy row (level + owner scope it applies to).

    level/owner combinations:
        ("global", None)                     — every account, every plugin
        ("account", "<account name>")        — one account (any plugin)
        ("plugin", "<plugin name>")          — one plugin (any account)
        ("account_plugin", "<acct>/<plugin>") — one account+plugin pair
    """

    level: str
    owner: str | None
    action: str
    interval: float
    jitter_min: float = 0.0
    jitter_max: float = 0.0

    def __post_init__(self) -> None:
        if self.level not in POLICY_LEVELS:
            raise ValueError(f"policy level {self.level!r} not in {POLICY_LEVELS}")
        if self.level == "global":
            self.owner = None
        elif not self.owner:
            raise ValueError(f"policy level {self.level!r} requires an owner")

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "owner": self.owner,
            "action": self.action,
            "interval": self.interval,
            "jitter": [self.jitter_min, self.jitter_max],
        }


@dataclass
class FleetGroupState:
    """Fleet projection for one group (PHASE 9): pause flag + the
    generation counter that fences stale dispatches (spec §5)."""

    group_name: str
    paused: bool = False
    generation: int = 1
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_name": self.group_name,
            "paused": self.paused,
            "generation": self.generation,
            "updated_at": round(self.updated_at, 3),
        }


@dataclass
class FleetMemberRecord:
    """One account's fleet state: execution mode (D1) + eligibility (§2)."""

    account: str
    group_name: str
    mode: str  # ExecutionMode value
    eligibility: str  # MemberEligibility value
    since: float = field(default_factory=time.time)
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "group_name": self.group_name,
            "mode": self.mode,
            "eligibility": self.eligibility,
            "since": round(self.since, 3),
            "version": self.version,
        }


@dataclass
class FleetAssignmentRecord:
    """The fleet work unit (§5) — an assignment drives scheduler tasks."""

    id: str
    group_name: str
    task_id: str
    status: str  # AssignmentStatus value
    account: str | None = None
    priority: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    stopped_at: float | None = None
    resume_policy: str = ""
    version: int = 1
    generation: int = 1
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group_name": self.group_name,
            "task_id": self.task_id,
            "status": self.status,
            "account": self.account,
            "priority": int(self.priority),
            "created_at": round(self.created_at, 3),
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "resume_policy": self.resume_policy,
            "version": self.version,
            "generation": self.generation,
            "error": self.error,
        }


@dataclass
class FleetCooldownRecord:
    """One scoped fleet cooldown row (§6); id is the row identity."""

    id: int
    scope_type: str
    scope_id: str
    reason: str
    started_at: float
    expires_at: float
    source: str = "manual"
    cleared_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "reason": self.reason,
            "started_at": round(self.started_at, 3),
            "expires_at": round(self.expires_at, 3),
            "source": self.source,
            "cleared_at": self.cleared_at,
        }


@dataclass
class FleetTransitionRecord:
    """One append-only history row (D2's modest transition table)."""

    id: int
    kind: str
    group_name: str
    generation: int
    reason: str
    correlation_id: str
    occurred_at: float
    task_id: str | None = None
    assignment_id: str | None = None
    account: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "group_name": self.group_name,
            "generation": self.generation,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
            "occurred_at": round(self.occurred_at, 3),
            "task_id": self.task_id,
            "assignment_id": self.assignment_id,
            "account": self.account,
        }


# --------------------------------------------------------------------------
# Workspace unification records (W2; migration v10 tables)
# --------------------------------------------------------------------------


@dataclass
class WorkspaceState:
    """A workspace row: identity + orchestration state in ONE owner
    (WORKSPACE_DESIGN §8 — generation/paused folded from fleet_groups)."""

    name: str
    description: str = ""
    enabled: bool = True
    paused: bool = False
    generation: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "paused": self.paused,
            "generation": self.generation,
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
        }


@dataclass
class WorkspaceMemberRecord:
    """The membership PROJECTION row (decision 11): written only in
    the same transaction as ``accounts.workspace``, never directly."""

    account: str
    workspace: str
    mode: str = "independent"
    eligibility: str = "eligible"
    joined_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "workspace": self.workspace,
            "mode": self.mode,
            "eligibility": self.eligibility,
            "joined_at": round(self.joined_at, 3),
            "updated_at": round(self.updated_at, 3),
            "version": self.version,
        }


@dataclass
class WorkspaceTaskRecord:
    """A work unit owned by a workspace (carried from fleet_assignments
    + the chain fields chain_run_id/step for W4)."""

    id: str
    workspace: str
    task_id: str
    account: str | None = None
    status: str = "queued"
    priority: int = 0
    chain_run_id: str | None = None
    step: int | None = None
    generation: int = 1
    error: str | None = None
    resume_policy: str = ""
    started_at: float | None = None
    stopped_at: float | None = None
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace": self.workspace,
            "task_id": self.task_id,
            "account": self.account,
            "status": self.status,
            "priority": self.priority,
            "chain_run_id": self.chain_run_id,
            "step": self.step,
            "generation": self.generation,
            "error": self.error,
            "resume_policy": self.resume_policy,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "version": self.version,
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
        }


@dataclass
class WorkspaceCooldownRecord:
    """A scoped policy cooldown (scope: workspace|member|task)."""

    id: int
    scope_type: str
    scope_id: str
    reason: str
    started_at: float
    expires_at: float
    source: str = "manual"
    cleared_at: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "reason": self.reason,
            "started_at": round(self.started_at, 3),
            "expires_at": round(self.expires_at, 3),
            "source": self.source,
            "cleared_at": self.cleared_at,
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
        }


@dataclass
class WorkspaceTransitionRecord:
    """One transition-history row (carried shape, renamed columns)."""

    id: int
    kind: str
    workspace: str
    generation: int
    reason: str
    correlation_id: str
    occurred_at: float
    task_id: str | None = None
    run_id: str | None = None
    account: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "workspace": self.workspace,
            "generation": self.generation,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
            "occurred_at": round(self.occurred_at, 3),
            "task_id": self.task_id,
            "run_id": self.run_id,
            "account": self.account,
        }


@dataclass
class PresetTemplateRecord:
    """A preset row (decisions 9/15): immutable versions under a
    stable preset_id; ``owner_id`` '' = global (SQLite NULL-unique
    workaround documented in migration v10)."""

    preset_id: int
    scope: str
    owner_id: str
    plugin: str
    name: str
    description: str = ""
    version: int = 1
    schema_version: int = 1
    settings: dict[str, Any] = field(default_factory=dict)
    archived: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset_id": self.preset_id,
            "scope": self.scope,
            "owner_id": self.owner_id or None,
            "plugin": self.plugin,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "schema_version": self.schema_version,
            "settings": dict(self.settings),
            "archived": self.archived,
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
        }


@dataclass
class AccountChannelRecord:
    """One registry row (§5.1/§5.2): IDENTITY + validity, never
    proof of usability (only 'available' resolves)."""

    account: str
    guild_id: str
    channel_id: str
    name: str = ""
    validity: str = "unknown"
    last_seen_at: float | None = None
    validation_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "name": self.name,
            "validity": self.validity,
            "last_seen_at": self.last_seen_at,
            "validation_reason": self.validation_reason,
        }


@dataclass
class PluginChannelBindingRecord:
    """One (account, plugin, channel) binding: the plugin's WHERE for
    that account (rule 2 — initialized by copy, then independent)."""

    account: str
    plugin: str
    channel_id: str
    guild_id: str = ""
    bound_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "plugin": self.plugin,
            "channel_id": self.channel_id,
            "guild_id": self.guild_id,
            "bound_at": round(self.bound_at, 3),
        }


@dataclass
class DeletionJobRecord:
    """One cross-store account-deletion job (decision 18): durable,
    retryable, observable — resumed at boot by the reconciler."""

    account: str
    state: str = "pending"
    attempts: int = 0
    last_error: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "state": self.state,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "created_at": round(self.created_at, 3),
            "completed_at": self.completed_at,
        }


__all__ = [
    "POLICY_LEVELS",
    "AccountChannelRecord",
    "AccountRecord",
    "DeletionJobRecord",
    "FleetAssignmentRecord",
    "FleetCooldownRecord",
    "FleetGroupState",
    "FleetMemberRecord",
    "FleetTransitionRecord",
    "PluginChannelBindingRecord",
    "PluginMetadata",
    "PluginProfileRecord",
    "PresetTemplateRecord",
    "RatePolicyRecord",
    "SyncGroupRecord",
    "WorkspaceCooldownRecord",
    "WorkspaceMemberRecord",
    "WorkspaceState",
    "WorkspaceTaskRecord",
    "WorkspaceTransitionRecord",
]
