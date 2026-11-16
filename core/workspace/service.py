"""WorkspaceService — the ONE writer (W3; supersedes FleetService at
W5, minus the seam closures the fleet design needed).

Discipline carried from the fleet service and the locked spec:
  * persist FIRST (database-authoritative, write-ahead), then publish
    (bus delivery is an optimization);
  * ONE command at a time under the service lock — the projection and
    the repo rows stay in agreement without a per-entity lock zoo;
  * membership is ONE transaction (decision 11: the pointer and the
    projection write together in ``set_account_workspace``);
  * a move invalidates stale workspace work (generation bump on leave
    fences it) WITHOUT deleting account-owned configuration (rules
    3-7: settings, data, channel assignments and bindings stay);
  * every state change goes through core.workspace.transitions
    validators — nothing else decides what a legal move is;
  * preset apply is a COPY with provenance under a stale-preview
    guard (decisions 9/15/16) — the pure core in core.workspace.
    .presets does the transforming, this service does the persisting.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from .models import (
    CooldownScopeType,
    CooldownSource,
    TaskStatus,
    WorkspaceEvent,
    WorkspaceTask,
)
from .presets import (
    StalePreview,
    compute_apply_diff,
    preview_revision_ok,
)
from .transitions import (
    InvalidTransition,
    validate_member_transition,
    validate_task_transition,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from database.models import PresetTemplateRecord, WorkspaceState
    from database.repositories.workspaces import WorkspaceRepository

__all__ = ["WorkspaceService"]

#: seams injected at construction (all optional; tests stub them,
#: bootstrap wires the real collaborators)
Publish = Callable[[WorkspaceEvent], Any]
Clock = Callable[[], float]
IdFactory = Callable[[], str]


class AccountsPointer(Protocol):
    """The account-existence surface membership needs."""

    async def get(self, name: str) -> Any: ...


class PacingRegistrar(Protocol):
    """The limiter's shared-clock mapping (decision 3: membership
    registers the account on the workspace clock; the engine's own
    API name carries until the W5 cleanup rename)."""

    def set_account_group(self, account: str, group: str | None) -> None: ...


class SettingsWriter(Protocol):
    """The plugin-settings seam preset apply writes through (the
    repository's upsert + read surface — the plugin manager's
    settings repo already satisfies this duck shape)."""

    async def set_setting(self, account: str, plugin: str, key: str, value: Any) -> Any: ...

    async def settings(self, account: str, plugin: str) -> dict[str, Any]: ...


class ProvenanceWriter(Protocol):
    """Provenance lives ON the assignment row (decision 9/D.4) — the
    account-plugins repository owns those columns."""

    async def set_provenance(
        self, account: str, plugin: str, preset_id: int, version: int
    ) -> None: ...

    async def clear_provenance(self, account: str, plugin: str) -> None: ...


class PluginDataStore(Protocol):
    """The separate documents store (deletion workflow step 4)."""

    async def drop_account(self, account: str) -> int: ...


class WorkspaceService:
    """Membership + orchestration + channels + presets: one writer.

    The constructor does NO I/O; ``await load()`` builds the
    in-memory projection from the repository (the database stays
    authoritative — every command commits before it publishes)."""

    def __init__(
        self,
        workspaces_repo: WorkspaceRepository,
        *,
        accounts: AccountsPointer | None = None,
        channels_repo: Any = None,
        presets_repo: Any = None,
        preset_defs: Any = None,  # P8 definitions + executions
        profiles: Any = None,  # account-owned profile store (P8 references)
        provenance: ProvenanceWriter | None = None,
        plugin_data: PluginDataStore | None = None,
        settings_writer: SettingsWriter | None = None,
        limiter: PacingRegistrar | None = None,
        publisher: Publish | None = None,
        time_fn: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._repo = workspaces_repo
        self._accounts = accounts
        self._channels = channels_repo
        self._presets = presets_repo
        self._provenance = provenance
        self._plugin_data = plugin_data
        self._settings = settings_writer
        self._limiter = limiter
        self._preset_defs = preset_defs
        self._profiles = profiles
        self._publisher = publisher
        self._now = time_fn or time.time
        self._new_id = id_factory or (lambda: uuid.uuid4().hex[:12])
        #: one command at a time: projection and rows agree without a
        #: per-entity lock zoo (spec: do not create a manager per noun)
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- boot

    async def load(self) -> dict[str, int]:
        """Projection boot: read back the authoritative rows."""
        async with self._lock:
            return await self._reload()

    async def sync_runtime(self) -> int:
        """Boot pacing restore: every persisted membership registers on
        its workspace's shared clock (decision 3 — the limiter mapping
        is runtime state that does not survive restarts)."""
        restored = 0
        for member in await self._repo.members():
            self._register_pacing(member.account, member.workspace)
            restored += 1
        return restored

    async def _reload(self) -> dict[str, int]:
        states = await self._repo.workspace_states()
        members = await self._repo.members()
        tasks = await self._repo.tasks()
        return {"workspaces": len(states), "members": len(members), "tasks": len(tasks)}

    # ------------------------------------------------------------- events

    async def _record(
        self,
        kind: str,
        workspace: str,
        reason: str,
        *,
        generation: int = 1,
        task_id: str | None = None,
        run_id: str | None = None,
        account: str | None = None,
    ) -> None:
        """Persist the transition, then publish (write-ahead)."""
        event = WorkspaceEvent(
            kind=kind,
            workspace=workspace,
            generation=generation,
            reason=reason,
            correlation_id=self._new_id(),
            occurred_at=self._now(),
            task_id=task_id,
            run_id=run_id,
            account=account,
        )
        await self._repo.append_transition(
            event.kind,
            event.workspace,
            event.generation,
            event.reason,
            event.correlation_id,
            event.occurred_at,
            task_id=event.task_id,
            run_id=event.run_id,
            account=event.account,
        )
        if self._publisher is not None:
            with contextlib.suppress(Exception):
                self._publisher(event)

    # ---------------------------------------------------------- workspace

    async def create_workspace(self, name: str, description: str = "") -> dict[str, Any]:
        async with self._lock:
            state = await self._repo.ensure_workspace(name)
            if description:
                await self._repo.rename_workspace(name, description)
                state = await self._repo.workspace_state(name) or state
            await self._record("WorkspaceCreated", name, "workspace created")
            created: dict[str, Any] = state.to_dict()
            return created

    async def workspace(self, name: str) -> dict[str, Any] | None:
        state = await self._repo.workspace_state(name)
        if state is None:
            return None
        members = await self._repo.members(name)
        return {
            **state.to_dict(),
            "member_count": len(members),
            "members": [m.account for m in members],
        }

    async def workspaces(self) -> list[dict[str, Any]]:
        out = []
        for state in await self._repo.workspace_states():
            members = await self._repo.members(state.name)
            out.append({**state.to_dict(), "member_count": len(members)})
        return out

    async def delete_workspace(self, name: str, *, detach_members: bool = False) -> dict[str, Any]:
        """Delete a workspace. Its ACCOUNTS are never deleted; a
        workspace with members is refused unless detach is explicit
        (members return to per-account pacing). Account-owned config
        is untouched by the detach (rules 3-7)."""
        async with self._lock:
            state = await self._repo.workspace_state(name)
            if state is None:
                raise ValueError(f"unknown workspace {name!r}")
            members = await self._repo.members(name)
            detached: list[str] = []
            if members:
                if not detach_members:
                    raise ValueError(
                        f"workspace {name!r} still has member(s)"
                        f" {[m.account for m in members]} — remove them or"
                        " pass detach_members=True"
                    )
                for member in members:
                    await self._leave_locked(member.account, name)
                    detached.append(member.account)
            existed = await self._repo.delete_workspace(name)
            await self._record(
                "WorkspaceDeleted",
                name,
                f"workspace deleted (detached={detached})" if detached else "workspace deleted",
            )
            return {"deleted": name, "existed": existed, "detached": detached}

    async def pause(self, name: str) -> dict[str, Any]:
        async with self._lock:
            state = await self._repo.set_workspace(name, paused=True)
            await self._record(
                "WorkspacePaused",
                name,
                "operator pause",
                generation=state.generation,
            )
            paused: dict[str, Any] = state.to_dict()
            return paused

    async def resume(self, name: str) -> dict[str, Any]:
        async with self._lock:
            state = await self._repo.set_workspace(name, paused=False)
            await self._record(
                "WorkspaceResumed",
                name,
                "operator resume",
                generation=state.generation,
            )
            resumed: dict[str, Any] = state.to_dict()
            return resumed

    async def bump_generation(self, name: str) -> int:
        """The stale-dispatch fence: invalidates approved-not-dispatched
        work (operator tool; leave/move calls it automatically)."""
        async with self._lock:
            state = await self._repo.bump_generation(name)
            await self._record(
                "GenerationBumped",
                name,
                "generation fence",
                generation=state.generation,
            )
            generation: int = state.generation
            return generation

    # ---------------------------------------------------------- membership

    async def join(self, account: str, workspace: str, *, mode: str = "managed") -> dict[str, Any]:
        """Put an account in a workspace (add or move — at-most-one by
        construction: the single pointer moves). Persist-first: the
        pointer+projection transaction commits, then pacing registers,
        then the event records."""
        async with self._lock:
            await self._require_account(account)
            state = await self._repo.ensure_workspace(workspace)
            existing = await self._repo.member(account)
            if existing is not None and existing.workspace == workspace:
                return {
                    "account": account,
                    "workspace": workspace,
                    "generation": state.generation,
                    "moved": False,
                }
            if existing is not None:
                # MOVE: fence the old workspace's stale work first
                await self._repo.set_account_workspace(account, workspace, mode=mode)
                await self._fence_member_tasks(account, existing.workspace)
                await self._repo.bump_generation(existing.workspace)
                await self._record(
                    "MemberMoved",
                    workspace,
                    f"{account} moved from {existing.workspace!r}",
                    generation=state.generation,
                    account=account,
                )
            else:
                await self._repo.set_account_workspace(account, workspace, mode=mode)
                await self._record(
                    "MemberJoined",
                    workspace,
                    f"{account} joined",
                    generation=state.generation,
                    account=account,
                )
            self._register_pacing(account, workspace)
            return {
                "account": account,
                "workspace": workspace,
                "generation": state.generation,
                "moved": existing is not None,
            }

    async def leave(self, account: str) -> dict[str, Any]:
        """Remove the account from any workspace. Account-owned config
        (settings, data, channel assignments, bindings) is NEVER
        touched — only membership and workspace-owned work."""
        async with self._lock:
            existing = await self._repo.member(account)
            if existing is None:
                return {"account": account, "workspace": None, "left": False}
            await self._leave_locked(account, existing.workspace)
            return {"account": account, "workspace": existing.workspace, "left": True}

    async def _leave_locked(self, account: str, workspace: str) -> None:
        """The leave path under the lock: pointer clears, stale work
        fences, pacing deregisters, eligibility retires, event records."""
        await self._repo.set_account_workspace(account, None)
        await self._fence_member_tasks(account, workspace)
        await self._repo.bump_generation(workspace)
        member = await self._repo.member(account)
        if member is not None:  # projection already cleared; nothing more
            pass
        self._register_pacing(account, None)
        await self._record(
            "MemberLeft",
            workspace,
            f"{account} left",
            account=account,
        )

    async def _fence_member_tasks(self, account: str, workspace: str) -> None:
        """Moving/leaving invalidates stale workspace work: the
        account's non-terminal tasks return to the queue (binding
        releases) — account-owned configuration is untouched."""
        mine = await self._repo.tasks(workspace=workspace, account=account)
        for task in mine:
            if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                continue
            try:
                validate_task_transition(task.status, TaskStatus.QUEUED)
            except InvalidTransition:
                continue  # terminal/forbidden shapes stay as history
            await self._repo.update_task(
                task.id,
                status=TaskStatus.QUEUED,
                account=None,
                stopped_at=self._now(),
            )

    def _register_pacing(self, account: str, workspace: str | None) -> None:
        """Membership drives the shared clock (decision 3). Tolerant of
        unwired stacks (pure metadata plumbing, no API involved)."""
        if self._limiter is None:
            return
        register = getattr(self._limiter, "set_account_group", None)
        if register is None:
            return
        with contextlib.suppress(Exception):
            register(account, workspace)

    async def _require_account(self, account: str) -> None:
        if self._accounts is not None:
            record = await self._accounts.get(account)
            if record is None:
                raise ValueError(f"unknown account {account!r}")

    # ------------------------------------------------------- eligibility

    async def set_member_eligibility(
        self, account: str, new: str, *, leaving: bool = False
    ) -> dict[str, Any]:
        async with self._lock:
            member = await self._repo.member(account)
            if member is None:
                raise ValueError(f"account {account!r} is not a workspace member")
            validate_member_transition(member.eligibility, new, leaving=leaving)
            await self._repo.update_member_eligibility(account, new)
            await self._record(
                "MemberEligibility",
                member.workspace,
                f"{account} {member.eligibility} -> {new}",
                account=account,
            )
            return {"account": account, "eligibility": new}

    # -------------------------------------------------------------- tasks

    async def create_task(
        self,
        workspace: str,
        task_id: str,
        *,
        priority: int = 0,
        resume_policy: str = "",
    ) -> str:
        """Enqueue a work unit (QUEUED, unassigned)."""
        async with self._lock:
            state = await self._repo.ensure_workspace(workspace)
            row_id = self._new_id()
            task = WorkspaceTask(
                id=row_id,
                workspace=workspace,
                task_id=task_id,
                status=TaskStatus.QUEUED,
                priority=priority,
                resume_policy=resume_policy,
                generation=state.generation,
            )
            await self._repo.insert_task(task)
            await self._record(
                "TaskCreated",
                workspace,
                f"task {task_id} queued",
                generation=state.generation,
                task_id=task_id,
            )
            return row_id

    async def assign_task(self, task_row_id: str, account: str) -> Any:
        """QUEUED -> ASSIGNED with the live generation (the fence rides
        the assignment: a stale generation is rejected at dispatch)."""
        async with self._lock:
            task = await self._require_task(task_row_id)
            state = await self._require_workspace(task.workspace)
            validate_task_transition(task.status, TaskStatus.ASSIGNED)
            await self._repo.update_task(
                task_row_id,
                status=TaskStatus.ASSIGNED,
                account=account,
                generation=state.generation,
                error=None,
            )
            await self._record(
                "TaskAssigned",
                task.workspace,
                f"task {task.task_id} assigned to {account}",
                generation=state.generation,
                task_id=task.task_id,
                account=account,
            )
            return await self._read_back(task_row_id)

    async def report_task_status(
        self,
        task_row_id: str,
        new: str,
        *,
        error: str | None = None,
        retry_allowed: bool = False,
    ) -> dict[str, Any]:
        """The shared terminal/transition path: validate -> persist ->
        record. FAILED -> QUEUED needs the explicit retry allowance."""
        async with self._lock:
            task = await self._require_task(task_row_id)
            validate_task_transition(task.status, new, retry_allowed=retry_allowed)
            fields: dict[str, Any] = {"status": new}
            if new == TaskStatus.RUNNING:
                fields["started_at"] = self._now()
            if new in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.SUSPENDED,
            ):
                fields["stopped_at"] = self._now()
                if new == TaskStatus.FAILED:
                    fields["error"] = error
            await self._repo.update_task(task_row_id, **fields)
            await self._record(
                f"Task{new.capitalize()}",
                task.workspace,
                error or f"task {task.task_id} {new}",
                task_id=task.task_id,
                account=task.account,
            )
            return await self._read_back(task_row_id)

    async def tasks(
        self,
        *,
        workspace: str | None = None,
        status: str | None = None,
        account: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = await self._repo.tasks(workspace=workspace, status=status, account=account)
        return [r.to_dict() for r in rows]

    async def _require_task(self, task_row_id: str) -> Any:
        task = await self._repo.task(task_row_id)
        if task is None:
            raise ValueError(f"unknown workspace task {task_row_id!r}")
        return task

    async def _require_workspace(self, name: str) -> WorkspaceState:
        state = await self._repo.workspace_state(name)
        if state is None:
            raise ValueError(f"unknown workspace {name!r}")
        found: WorkspaceState = state
        return found

    async def _read_back(self, task_row_id: str) -> dict[str, Any]:
        """Re-read after write — a vanished row is an integrity
        failure to surface, never a None to smuggle (W1 discipline)."""
        task = await self._repo.task(task_row_id)
        if task is None:
            raise ValueError(f"workspace task {task_row_id!r} vanished mid-command")
        return task.to_dict()

    # ---------------------------------------------------------- cooldowns

    async def start_cooldown(
        self,
        scope_type: str,
        scope_id: str,
        reason: str,
        duration_s: float,
        *,
        source: str = CooldownSource.MANUAL,
    ) -> dict[str, Any]:
        if scope_type not in CooldownScopeType.ALL:
            raise ValueError(f"unknown cooldown scope {scope_type!r}")
        if duration_s <= 0:
            raise ValueError(f"duration_s must be positive, got {duration_s!r}")
        async with self._lock:
            now = self._now()
            row_id = await self._repo.insert_cooldown(
                scope_type, scope_id, reason, now, now + duration_s, source
            )
            workspace = scope_id if scope_type == CooldownScopeType.WORKSPACE else ""
            await self._record(
                "CooldownStarted",
                workspace or "-",
                f"{scope_type} cooldown: {reason}",
            )
            record = await self._repo.cooldown(row_id)
            assert record is not None
            started: dict[str, Any] = record.to_dict()
            return started

    async def clear_cooldown(self, cooldown_id: int) -> dict[str, Any]:
        """Clear a MANUAL cooldown only: observed/server windows are
        the platform's own truth and ride to expiry (carried rule)."""
        async with self._lock:
            record = await self._repo.cooldown(cooldown_id)
            if record is None:
                raise ValueError(f"unknown cooldown {cooldown_id!r}")
            if record.cleared_at is not None:
                raise ValueError(f"cooldown {cooldown_id} is already cleared")
            if record.source != CooldownSource.MANUAL:
                raise ValueError(
                    f"cooldown {cooldown_id} is source={record.source!r} —"
                    " only manual cooldowns clear"
                )
            await self._repo.clear_cooldown(cooldown_id, self._now())
            cleared = await self._repo.cooldown(cooldown_id)
            assert cleared is not None
            cleared_view: dict[str, Any] = cleared.to_dict()
            return cleared_view

    async def cooldowns(
        self,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        rows = await self._repo.cooldowns(
            scope_type=scope_type, scope_id=scope_id, active_only=active_only, now=self._now()
        )
        return [r.to_dict() for r in rows]

    # ------------------------------------------------------------ channels

    async def assign_channel(self, account: str, guild_id: str, channel_id: str) -> None:
        """Add ONE channel to the account's selection (the §14
        add-one verb, distinct from any replace-set surface)."""
        async with self._lock:
            await self._channels_upsert(account, guild_id, channel_id)
            await self._channels.assign_channel(account, guild_id, channel_id)

    async def unassign_channel(self, account: str, channel_id: str) -> bool:
        async with self._lock:
            return bool(await self._channels.unassign_channel(account, channel_id))

    async def bind_plugin_channels_from_assignments(self, account: str, plugin: str) -> int:
        """Initialize the plugin's bindings by COPY from the account's
        assignments (rule 2) — then they diverge freely."""
        async with self._lock:
            count = 0
            for channel in await self._channels.assignments(account):
                await self._channels.bind_plugin_channel(
                    account, plugin, channel.channel_id, guild_id=channel.guild_id
                )
                count += 1
            return count

    async def bind_plugin_channel(
        self, account: str, plugin: str, channel_id: str, *, guild_id: str = ""
    ) -> None:
        async with self._lock:
            await self._channels.bind_plugin_channel(account, plugin, channel_id, guild_id=guild_id)

    async def unbind_plugin_channel(self, account: str, plugin: str, channel_id: str) -> bool:
        async with self._lock:
            return bool(await self._channels.unbind_plugin_channel(account, plugin, channel_id))

    async def account_channels(self, account: str) -> list[dict[str, Any]]:
        return [c.to_dict() for c in await self._channels.channels(account)]

    async def plugin_bindings(self, account: str, plugin: str) -> list[dict[str, Any]]:
        return [b.to_dict() for b in await self._channels.plugin_bindings(account, plugin)]

    async def _channels_upsert(self, account: str, guild_id: str, channel_id: str) -> None:
        """Assignments reference registry entries: registering a channel
        for assignment ensures its registry row exists (identity-first)."""
        existing = await self._channels.channel(account, guild_id, channel_id)
        if existing is None:
            await self._channels.upsert_channel(account, guild_id, channel_id)

    # ------------------------------------------------------------- presets

    async def preset_preview(
        self,
        account: str,
        plugin: str,
        preset_id: int,
        version: int | None = None,
    ) -> dict[str, Any]:
        """The §6.4 diff + revision fingerprint for confirmation."""
        template = await self._resolve_preset_version(preset_id, version)
        writer = self._require_settings()
        current = await writer.settings(account, plugin)
        diff = compute_apply_diff(template.settings, current)
        return {
            "preset_id": preset_id,
            "version": template.version,
            "diff": {k: [old, new] for k, (old, new) in diff.items()},
            "revision": self._revision_fingerprint(current),
        }

    async def preset_apply(
        self,
        account: str,
        plugin: str,
        preset_id: int,
        *,
        preview_revision: str,
        version: int | None = None,
    ) -> dict[str, Any]:
        """COPY-ON-APPLY (decisions 9/15/16): validate the immutable
        version, guard the revision, write the preset's keys only,
        record provenance on the assignment row."""
        template = await self._resolve_preset_version(preset_id, version)
        writer = self._require_settings()
        current = await writer.settings(account, plugin)
        try:
            preview_revision_ok(preview_revision, self._revision_fingerprint(current))
        except StalePreview as exc:
            return {"applied": False, "conflict": str(exc)}
        for key, value in template.settings.items():
            await writer.set_setting(account, plugin, key, value)
        await self._require_provenance().set_provenance(
            account, plugin, template.preset_id, template.version
        )
        after = await writer.settings(account, plugin)
        return {
            "applied": True,
            "preset_id": preset_id,
            "version": template.version,
            "revision": self._revision_fingerprint(after),
        }

    async def preset_detach(self, account: str, plugin: str) -> dict[str, Any]:
        """§6.3 detach: values stay, provenance link drops."""
        await self._require_provenance().clear_provenance(account, plugin)
        return {"account": account, "plugin": plugin, "detached": True}

    # --------------------------------------- P8: reference-based presets
    async def preset_define(
        self,
        workspace: str,
        name: str,
        *,
        participants: list[dict[str, str]],
        order: list[str] | None = None,
    ) -> dict[str, Any]:
        """Define a reusable orchestration: REFERENCES, never copies.

        ``participants`` are (account, plugin, profile) tuples the preset
        coordinates; ``order`` is the execution order (account names).
        The definition NEVER stores profile configuration — applying
        validates the references and activates the profiles in place.
        """
        if self._preset_defs is None:
            raise RuntimeError("workspace preset definitions are not wired")
        definition: dict[str, Any] = {"participants": participants}
        if order is not None:
            definition["order"] = order
        preset_id = await self._preset_defs.define(workspace, name, definition)
        return {"preset_id": preset_id, "workspace": workspace, "name": name}

    async def preset_apply_references(self, preset_id: int) -> dict[str, Any]:
        """Validate every referenced profile and ACTIVATE it in place.

        Fail-closed on drift (a renamed/deleted profile is DETECTED,
        never silently substituted) and on non-membership (a preset may
        only reference the workspace's member accounts). No
        configuration is copied anywhere — activation is the whole side
        effect.
        """
        if self._preset_defs is None or self._profiles is None:
            raise RuntimeError("workspace preset definitions are not wired")
        record = await self._preset_defs.get(preset_id)
        if record is None:
            raise ValueError(f"unknown preset id {preset_id!r}")
        workspace = record["workspace"]
        participants = record["definition"].get("participants") or []
        members = await self._repo.members(workspace)
        member_names = {m.account for m in members}
        activated: list[str] = []
        for ref in participants:
            account, plugin, profile = (
                ref.get("account", ""),
                ref.get("plugin", ""),
                ref.get("profile", ""),
            )
            if account not in member_names:
                raise ValueError(
                    f"preset references non-member account {account!r}"
                    f" (members: {sorted(member_names)})"
                )
            if not await self._profiles.exists(account, plugin, profile):
                raise ValueError(
                    f"referenced profile {profile!r} does not exist for"
                    f" {plugin!r} on {account!r} — renamed or deleted"
                    f" (drift detected, nothing was substituted)"
                )
            await self._profiles.set_active(account, plugin, profile)
            activated.append(f"{account}/{plugin}/{profile}")
        return {"preset_id": preset_id, "workspace": workspace, "activated": activated}

    async def execution_start(self, preset_id: int) -> dict[str, Any]:
        """Start (or restart) one execution instance of the definition."""
        import uuid

        if self._preset_defs is None:
            raise RuntimeError("workspace preset definitions are not wired")
        record = await self._preset_defs.get(preset_id)
        if record is None:
            raise ValueError(f"unknown preset id {preset_id!r}")
        execution_id = f"exec-{uuid.uuid4().hex[:12]}"
        await self._preset_defs.start_execution(record["workspace"], preset_id, execution_id)
        return {
            "execution_id": execution_id,
            "preset_id": preset_id,
            "state": "running",
        }

    async def execution_checkpoint(
        self, execution_id: str, account: str, state: str
    ) -> dict[str, Any]:
        """Record a completed participant (the resume point)."""
        if self._preset_defs is None:
            raise RuntimeError("workspace preset definitions are not wired")
        run = await self._preset_defs.execution(execution_id)
        if run is None:
            raise ValueError(f"unknown execution {execution_id!r}")
        if state == "done" and account not in run["completed"]:
            run["completed"].append(account)
        await self._preset_defs.checkpoint(execution_id, run["completed"])
        return {"execution_id": execution_id, "completed": run["completed"]}

    async def execution_resume(self, execution_id: str) -> dict[str, Any]:
        """The resume view: completed participants are NOT replayed."""
        if self._preset_defs is None:
            raise RuntimeError("workspace preset definitions are not wired")
        run = await self._preset_defs.execution(execution_id)
        if run is None:
            raise ValueError(f"unknown execution {execution_id!r}")
        record = await self._preset_defs.get(run["preset_id"])
        order = (record or {}).get("definition", {}).get("order") if record else None
        if order:
            pending = [a for a in order if a not in run["completed"]]
        else:
            done = set(run["completed"])
            pending = [
                p["account"]
                for p in (record or {}).get("definition", {}).get("participants", [])
                if p["account"] not in done
            ]
        return {
            "execution_id": execution_id,
            "completed": run["completed"],
            "pending": pending,
        }

    async def _resolve_preset_version(
        self, preset_id: int, version: int | None
    ) -> PresetTemplateRecord:
        if version is None:
            template = await self._presets.get(preset_id)
        else:
            template = await self._presets.version(preset_id, version)
        if template is None:
            raise ValueError(
                f"unknown preset {preset_id!r}"
                + (f" version {version!r}" if version is not None else "")
            )
        resolved: PresetTemplateRecord = template
        return resolved

    @staticmethod
    def _revision_fingerprint(current: Mapping[str, Any]) -> str:
        """The decision-16 precondition: any intervening settings write
        changes the fingerprint (content hash over sorted keys)."""
        canonical = json.dumps({k: current[k] for k in sorted(current)}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def _require_settings(self) -> SettingsWriter:
        """Preset operations need the settings seam — an unwired stack
        fails loudly, not with a None attribute error."""
        if self._settings is None:
            raise RuntimeError("preset operations need a settings writer (unwired stack)")
        return self._settings

    def _require_provenance(self) -> ProvenanceWriter:
        if self._provenance is None:
            raise RuntimeError("preset operations need the provenance writer (unwired stack)")
        return self._provenance

    # ---------------------------------------------- account removal (§13.3)

    async def forget_account(self, account: str) -> dict[str, Any]:
        """The cross-store deletion workflow (decision 18): durable,
        retryable, observable. Primary store first, then the separate
        documents store; failures quarantine, never silently abandon."""
        async with self._lock:
            job = await self._repo.create_deletion_job(account)
            assert job is not None
            try:
                # the account's live workspace tasks die FIRST (while the
                # account binding still exists to find them — leaving
                # requeues and releases the binding)
                for task in await self._repo.tasks(account=account):
                    if task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                        await self._repo.update_task(
                            task.id,
                            status=TaskStatus.CANCELLED,
                            stopped_at=self._now(),
                            error="account removed",
                        )
                # membership leaves (fences stale work; config stays —
                # it dies with the account's own rows below)
                member = await self._repo.member(account)
                if member is not None:
                    await self._leave_locked(account, member.workspace)
                # channel rows (registry + assignments + bindings)
                if self._channels is not None:
                    await self._channels.drop_account(account)
                await self._repo.update_deletion_job(account, state="primary_done")
                # the separate store: idempotent, per-account
                if self._plugin_data is not None:
                    await self._plugin_data.drop_account(account)
                await self._repo.update_deletion_job(account, state="complete")
                return {"account": account, "state": "complete"}
            except Exception as exc:
                await self._repo.update_deletion_job(
                    account, state="quarantined", last_error=str(exc)
                )
                return {"account": account, "state": "quarantined", "error": str(exc)}
