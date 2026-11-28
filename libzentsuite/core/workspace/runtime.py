"""ChainRuntime — the orchestrator (W4; WORKSPACE_DESIGN §9-10).

THE headline feature: account A does something, a follow-up account B
cleans it up — if settings/plugins allow it. The runtime is the
consumer side of the trigger catalog:

  * MECHANICAL triggers: terminal task events (plugin + action +
    completed/failed) matched against step ``on`` bindings;
  * SEMANTIC triggers: accepted signals (``<plugin>:<name>``) matched
    the same way.

Execution contract (decisions 13/14 + §10):
  * a run captures an IMMUTABLE definition snapshot at creation;
  * steps execute SEQUENTIALLY; every step gets a step_executions row
    BEFORE execution — target persisted first (retries and recovery
    reuse it, never re-select);
  * ``first_eligible`` is deterministic: membership seniority
    (joined_at ASC), account name ASC tie-break;
  * every target passes the W1 eligibility gate (the 7-check policy);
    a denied step is a VISIBLE SKIPPED with the policy's reason —
    never a silent drop, never a blind send;
  * ``on_skip``: continue (default) carries the run; abort fails it;
  * PARTIAL is first-class.

Signals AUTHORIZE NOTHING (carried rule): they are observations the
runtime matches; the eligibility gate decides.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any, Protocol

from .models import (
    ChainDefinition,
    ChainStep,
    PolicyDecision,
    RoutingPolicy,
    StepStatus,
)
from .policy import DispatchFacts, WorkspacePolicy

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["ChainRuntime", "EligibilityReader", "HandlerInvoker", "TerminalTrigger"]


class HandlerInvoker(Protocol):
    """Invoke one plugin's follow-handler on one account.

    Production wiring: the plugin manager resolves the per-(account,
    plugin) instance, builds its context, and calls the async handler
    with ``(ctx, payload)`` — the handler's sends ride the paced
    pipeline like any plugin code."""

    async def __call__(
        self, account: str, plugin: str, handler: str, payload: dict[str, Any]
    ) -> Any: ...


class EligibilityReader(Protocol):
    """The check-7 facts reader: plugin assigned/enabled on the
    account, the gate condition, channel-binding usability."""

    async def plugin_gate_facts(
        self, account: str, plugin: str, gate: dict[str, Any]
    ) -> dict[str, Any]: ...


class TerminalTrigger:
    """A matched mechanical trigger (task-terminal event shape)."""

    __slots__ = ("account", "action", "kind", "plugin")

    def __init__(self, account: str, plugin: str, action: str, kind: str) -> None:
        self.account = account
        self.plugin = plugin
        self.action = action
        self.kind = kind  # "task_completed" | "task_failed"


class ChainRuntime:
    """Match triggers to chains; execute runs over immutable snapshots."""

    def __init__(
        self,
        repo: Any,
        *,
        service: Any,  # WorkspaceService (state facts + skip recording)
        outbox: Any = None,  # SignalOutbox (emitting step.emit)
        signal_reader: Any = None,  # SignalsRepository (reading by id)
        invoker: HandlerInvoker | None = None,
        gate_reader: EligibilityReader | None = None,
        policy: WorkspacePolicy | None = None,
        time_fn: Callable[[], float] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repo = repo
        self._service = service
        self._outbox = outbox
        self._signal_reader = signal_reader
        self._invoker = invoker
        self._gate_reader = gate_reader
        self._policy = policy or WorkspacePolicy()
        self._now = time_fn or time.time
        self._new_id = id_factory or (lambda: uuid.uuid4().hex[:12])

    # ------------------------------------------------------ definitions

    @staticmethod
    def parse_definition(definition: dict[str, Any]) -> ChainDefinition:
        """Validate through the domain model (InvalidDefinition names
        the exact problem — the loader/CLI renders it verbatim). Pure:
        callers without a runtime instance (the live-debug harness
        validating a definition BEFORE persisting it) can use it
        directly."""
        steps = tuple(
            ChainStep(
                position=int(s.get("position", 0)),
                plugin=str(s.get("plugin", "")),
                routing=str(s.get("routing", RoutingPolicy.FIRST_ELIGIBLE)),
                target=str(s.get("target", "")),
                on=dict(s.get("on") or {}),
                gate=dict(s.get("gate") or {}),
                emit=str(s.get("emit", "")),
            )
            for s in (definition.get("steps") or [])
        )
        return ChainDefinition(
            name=str(definition.get("name", "")),
            steps=steps,
            on_skip=str(definition.get("on_skip", "continue")),
        )

    async def create_chain(self, workspace: str, definition: dict[str, Any]) -> dict[str, Any]:
        parsed = self.parse_definition(definition)
        chain_id = await self._repo.insert_chain(workspace, parsed.name, json.dumps(definition))
        row = await self._repo.chain(chain_id)
        assert row is not None
        created: dict[str, Any] = dict(row)
        return created

    async def update_chain(self, chain_id: int, definition: dict[str, Any]) -> dict[str, Any]:
        """Edit: revision bumps; existing runs keep their snapshots."""
        self.parse_definition(definition)
        await self._repo.update_chain_definition(chain_id, json.dumps(definition))
        row = await self._repo.chain(chain_id)
        assert row is not None
        updated: dict[str, Any] = dict(row)
        return updated

    # ---------------------------------------------------- trigger intake

    async def on_terminal(self, trigger: TerminalTrigger) -> list[str]:
        """Mechanical intake: every enabled chain whose FIRST step
        matches starts a run. Returns the started run ids."""
        return await self._fire_matching(
            lambda step: self._matches_terminal(step, trigger),
            triggered_by=f"task:{trigger.account}:{trigger.plugin}",
        )

    async def on_signal(self, signal_id: str) -> list[str]:
        """Semantic intake: an accepted signal fires matching chains.
        Idempotent: a redelivered signal finds the first steps already
        past their trigger (runs exist), and ``_fire_matching`` only
        ever starts NEW runs from a trigger — the dedupe companion is
        the run's triggered_by + step state."""
        if self._signal_reader is None:
            return []
        signal = await self._signal_reader.signal(signal_id)
        if signal is None:
            return []
        namespaced = signal.namespaced
        return await self._fire_matching(
            lambda step: bool(step.on.get("signal")) and step.on["signal"] == namespaced,
            triggered_by=f"signal:{signal_id}",
        )

    @staticmethod
    def _matches_terminal(step: ChainStep, trigger: TerminalTrigger) -> bool:
        on = step.on
        if not on.get("task_action"):
            return False
        if on.get("plugin") != trigger.plugin:
            return False
        if on.get("task_action") != trigger.action:
            return False
        return on.get("event", "task_completed") == trigger.kind

    async def _fire_matching(self, matches: Any, *, triggered_by: str) -> list[str]:
        started: list[str] = []
        for row in await self._repo.chains():
            if not row.get("enabled"):
                continue
            definition = self.parse_definition(json.loads(row["definition_json"]))
            if not definition.steps or not matches(definition.steps[0]):
                continue
            run_id = await self.start_run(row["workspace"], row["id"], triggered_by=triggered_by)
            if run_id is not None:
                started.append(run_id)
        return started

    # -------------------------------------------------------- execution

    async def start_run(
        self, workspace: str, chain_id: int, *, triggered_by: str = ""
    ) -> str | None:
        """Create a run over the IMMUTABLE snapshot, then execute it.
        None when the chain is unknown or disabled."""
        row = await self._repo.chain(chain_id)
        if row is None or not row.get("enabled"):
            return None
        definition = self.parse_definition(json.loads(row["definition_json"]))
        run_id = self._new_id()
        await self._repo.insert_chain_run(
            run_id,
            chain_id,
            workspace,
            row["definition_json"],
            triggered_by=triggered_by,
        )
        await self._execute_run(run_id, workspace, definition)
        return run_id

    async def _execute_run(self, run_id: str, workspace: str, definition: ChainDefinition) -> None:
        outcomes: list[str] = []
        for step in definition.steps:
            outcome = await self._execute_step(run_id, workspace, step)
            outcomes.append(outcome)
            if outcome == StepStatus.FAILED and definition.on_skip == "abort":
                await self._repo.finish_chain_run(run_id, "failed")
                return
        if StepStatus.FAILED in outcomes:
            final = "failed" if definition.on_skip == "abort" else "partial"
        elif StepStatus.SKIPPED in outcomes:
            final = "partial"
        else:
            final = "completed"
        await self._repo.finish_chain_run(run_id, final)

    async def _execute_step(self, run_id: str, workspace: str, step: ChainStep) -> str:
        """One step: route -> persist -> gate -> invoke -> record.

        The step_executions row exists BEFORE execution (decision 13's
        persisted target); a denial finishes that row as a VISIBLE
        SKIPPED carrying the policy's reason."""
        target, route_reason = await self._route(workspace, step)
        execution_id = await self._repo.insert_step_execution(run_id, step.position, target or "")
        if target is None:
            await self._repo.finish_step_execution(
                execution_id, StepStatus.SKIPPED, route_reason or "no eligible member"
            )
            return StepStatus.SKIPPED
        verdict = await self._gate(workspace, target, step)
        if not verdict.allowed:
            await self._repo.finish_step_execution(execution_id, StepStatus.SKIPPED, verdict.reason)
            return StepStatus.SKIPPED
        if self._invoker is None:
            await self._repo.finish_step_execution(
                execution_id, StepStatus.SKIPPED, "no handler invoker wired"
            )
            return StepStatus.SKIPPED
        payload = {
            "run_id": run_id,
            "position": step.position,
            "trigger": dict(step.on),
        }
        try:
            await self._invoker(target, step.plugin, self._handler_for(step), payload)
        except Exception as exc:
            await self._repo.finish_step_execution(execution_id, StepStatus.FAILED, str(exc))
            return StepStatus.FAILED
        await self._repo.finish_step_execution(execution_id, StepStatus.DONE, "")
        if step.emit and self._outbox is not None:
            await self._outbox.accept(target, step.plugin, step.emit, payload)
        return StepStatus.DONE

    @staticmethod
    def _handler_for(step: ChainStep) -> str:
        """The follow-handler name: the step's gate may name it; the
        default convention mirrors the plugin declarations
        (FOLLOWS entries carry the authoritative handler)."""
        return str(step.gate.get("handler") or f"on_{step.plugin}_trigger")

    async def _route(self, workspace: str, step: ChainStep) -> tuple[str | None, str]:
        """Decision 13: specific_account uses the configured member;
        first_eligible picks deterministically (seniority, then name)
        among members whose gate PASSES. Returns (target, reason):
        when nobody passes, the reason is the SENIOR candidate's
        denial (the most informative visible skip), not a generic
        'no member'."""
        if step.routing == RoutingPolicy.SPECIFIC_ACCOUNT:
            member = await self._repo.member(step.target)
            if member is None or member.workspace != workspace:
                return None, f"configured member {step.target!r} is not in this workspace"
            return step.target, ""
        candidates = sorted(
            (m for m in await self._repo.members(workspace) if m.eligibility == "eligible"),
            key=lambda m: (m.joined_at, m.account),
        )
        first_denial = ""
        for member in candidates:
            verdict = await self._gate(workspace, member.account, step)
            if verdict.allowed:
                account_name: str = member.account
                return account_name, ""
            if not first_denial:
                first_denial = f"{member.account}: {verdict.reason}"
        if not candidates:
            return None, "no eligible member"
        return None, f"no gate-passing member (senior denial — {first_denial})"

    async def _gate(self, workspace: str, account: str, step: ChainStep) -> PolicyDecision:
        """The 7-check policy over gathered facts (checks 1-6 from the
        projection; check 7 through the reader seam — an absent reader
        degrades permissively, the same seam discipline as the fleet
        engine's async readers)."""
        state = await self._repo.workspace_state(workspace)
        member = await self._repo.member(account)
        if state is None or member is None:
            return PolicyDecision(allowed=False, reason=f"unknown workspace/member for {account!r}")
        from .models import CooldownRecord

        cooldowns: list[Any] = []
        for scope_type, scope_id in (("workspace", workspace), ("member", account)):
            for row in await self._repo.cooldowns(
                scope_type=scope_type, scope_id=scope_id, active_only=True, now=self._now()
            ):
                cooldowns.append(
                    CooldownRecord(
                        scope_type=row.scope_type,
                        scope_id=row.scope_id,
                        reason=row.reason,
                        started_at=row.started_at,
                        expires_at=row.expires_at,
                        source=row.source,
                        id=row.id,
                    )
                )
        gate_facts: dict[str, Any] = {}
        if self._gate_reader is not None:
            gate_facts = dict(
                await self._gate_reader.plugin_gate_facts(account, step.plugin, step.gate)
            )
        facts = DispatchFacts(
            framework_safe=True,
            workspace_name=workspace,
            workspace_enabled=bool(state.enabled),
            workspace_paused=bool(state.paused),
            workspace_generation=state.generation,
            member_eligibility=member.eligibility,
            account_connected=True,  # dispatch-time; the guard owns live reads
            cooldowns=tuple(cooldowns),
            now=self._now(),
            account=account,
            task_label=f"chain:{step.plugin}:{step.position}",
            plugin_assigned=gate_facts.get("plugin_assigned", True),
            plugin_enabled=gate_facts.get("plugin_enabled", True),
            gate_passed=gate_facts.get("gate_passed", True),
            channel_binding_usable=gate_facts.get("channel_binding_usable", True),
            channel_binding_reason=str(gate_facts.get("channel_binding_reason", "")),
        )
        return self._policy.evaluate(facts)

    # ----------------------------------------------------- reconciliation

    async def reconcile_interrupted_runs(self) -> int:
        """Restart recovery: a RUNNING run at boot was interrupted —
        its unfinished steps fail with a classified recovery error
        (never blindly re-executed; explicit retry is the operator's
        call, §10.4)."""
        repaired = 0
        for run in await self._repo.chain_runs(status="running"):
            for ex in await self._repo.step_executions(run["id"]):
                if not ex["outcome"]:
                    await self._repo.finish_step_execution(
                        ex["id"],
                        StepStatus.FAILED,
                        "interrupted by restart — explicit retry required",
                    )
                    repaired += 1
            await self._repo.finish_chain_run(run["id"], "failed")
        return repaired

    # -------------------------------------------------------------- reads

    async def runs(self, workspace: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await self._repo.chain_runs(workspace=workspace)
        return rows

    async def run_steps(self, run_id: str) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = await self._repo.step_executions(run_id)
        return steps
