"""The workspace dispatch guard + reconciler (W4).

The guard answers the scheduler's pre-dispatch question — "may this
account dispatch now?" — through the W1 policy over live facts. It
carries the fleet guard's two boundaries: the limiter still gates
every dispatch AFTER it (decides WHETHER, never WHEN), and a
non-member account is none of its business (independent dispatch
untouched).

The reconciler is the 30-second heartbeat: it drives the outbox
delivery sweep, resumes the durable deletion jobs (decision 18 — the
workflow survives process death), repairs interrupted chain runs
(never blindly replaying), and purges old signals on a slow schedule.
Every pass is idempotent: a healthy sweep changes nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from .models import PolicyDecision
from .policy import DispatchFacts, WorkspacePolicy

if TYPE_CHECKING:
    from collections.abc import Callable

    from .runtime import ChainRuntime
    from .service import WorkspaceService
    from .signals import SignalOutbox

__all__ = ["WorkspaceDispatchGuard", "WorkspaceReconciler"]

LOG = logging.getLogger("libzent.workspace")


class WorkspaceDispatchGuard:
    """The scheduler seam: async guard(task) -> duck PolicyDecision.

    Reads membership + workspace state + cooldowns from the service's
    repository; non-members pass untouched; members get checks 1-6
    (framework safety stays the scheduler's own halt state, check 7's
    plugin facts are routing-time — dispatch already carries a routed
    task, so the gate's denial there was the visible skip)."""

    def __init__(
        self,
        service: WorkspaceService,
        *,
        policy: WorkspacePolicy | None = None,
        scheduler_halted: Callable[[], bool] | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._service = service
        self._repo = service._repo
        self._policy = policy or WorkspacePolicy()
        self._halted = scheduler_halted or (lambda: False)
        self._now = time_fn or time.time

    async def __call__(self, task: Any) -> PolicyDecision:
        account = str(getattr(task, "account", "") or "")
        member = await self._repo.member(account)
        if member is None:
            return PolicyDecision(allowed=True, reason="not workspace-managed")
        state = await self._repo.workspace_state(member.workspace)
        if state is None:  # projection/stale drift — fail closed for members
            return PolicyDecision(
                allowed=False, reason=f"workspace {member.workspace!r} has no state (fail closed)"
            )
        from .models import CooldownRecord

        cooldowns: list[Any] = []
        for scope_type, scope_id in (
            ("workspace", member.workspace),
            ("member", account),
        ):
            for row in await self._repo.cooldowns(
                scope_type=scope_type,
                scope_id=scope_id,
                active_only=True,
                now=self._now(),
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
        facts = DispatchFacts(
            framework_safe=not self._halted(),
            workspace_name=member.workspace,
            workspace_enabled=bool(state.enabled),
            workspace_paused=bool(state.paused),
            workspace_generation=state.generation,
            member_eligibility=member.eligibility,
            account_connected=True,  # the account manager already routed it
            cooldowns=tuple(cooldowns),
            now=self._now(),
            account=account,
            task_label=str(getattr(task, "name", "") or getattr(task, "id", "")),
        )
        return self._policy.evaluate(facts)


class WorkspaceReconciler:
    """Idempotent convergence passes (W4; the fleet reconciler's
    discipline carried): outbox delivery, deletion-job resumption,
    interrupted-run repair, signal retention."""

    def __init__(
        self,
        service: WorkspaceService,
        *,
        runtime: ChainRuntime | None = None,
        outbox: SignalOutbox | None = None,
        plugin_data: Any = None,
        channels_repo: Any = None,
        interval_s: float = 30.0,
        purge_interval_s: float = 3600.0,
    ) -> None:
        self._service = service
        self._repo = service._repo
        self._runtime = runtime
        self._outbox = outbox
        self._plugin_data = plugin_data
        self._channels = channels_repo
        self._interval = float(interval_s)
        self._purge_every = float(purge_interval_s)
        self._task: asyncio.Task[None] | None = None
        self._last_purge = 0.0

    # ----------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="workspace-reconciler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        # boot pass before the cadence (the fleet reconciler's rule)
        with contextlib.suppress(Exception):
            await self.reconcile()
        while True:
            await asyncio.sleep(self._interval)
            with contextlib.suppress(asyncio.CancelledError):
                raise
            with contextlib.suppress(Exception):
                await self.reconcile()

    # -------------------------------------------------------------- passes

    async def reconcile(self) -> dict[str, int]:
        """One idempotent pass; returns what it did (zeros = healthy)."""
        did: dict[str, int] = {}
        did["signals_delivered"] = await self._sweep_outbox()
        did["deletions_resumed"] = await self._resume_deletions()
        did["runs_repaired"] = (
            await self._runtime.reconcile_interrupted_runs() if self._runtime is not None else 0
        )
        did["signals_purged"] = await self._maybe_purge()
        if any(did.values()):
            LOG.info("reconciler pass: %s", did)
        return did

    async def _sweep_outbox(self) -> int:
        if self._outbox is None:
            return 0
        result = await self._outbox.deliver_pending()
        return int(result.get("delivered", 0))

    async def _resume_deletions(self) -> int:
        """Decision 18: a job stuck in pending/primary_done (the process
        died mid-workflow) resumes — the steps are idempotent by
        construction, so re-running forget_account is safe."""
        resumed = 0
        for job in await self._repo.deletion_jobs(state="pending") + await self._repo.deletion_jobs(
            state="primary_done"
        ):
            outcome = await self._service.forget_account(job.account)
            if outcome.get("state") in ("complete", "quarantined"):
                resumed += 1
        return resumed

    async def _maybe_purge(self) -> int:
        now = time.time()
        if now - self._last_purge < self._purge_every or self._outbox is None:
            return 0
        self._last_purge = now
        return await self._outbox.purge()
