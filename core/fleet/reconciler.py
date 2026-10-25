"""FleetReconciler — correctness never rides the event bus (§9, G).

Runs at BOOT, on demand, and every 30s: compares the persisted fleet
projection against what should be true and repairs what is SAFE —
through the service's transition-checked operations, so every repair
is idempotent (a healthy pass changes nothing) and auditable (each
move lands in the transition history).

Repairs:
  * crash recovery — RUNNING assignments at boot/reconcile suspend and
    requeue, and the group generation bumps: a zombie process's stale
    dispatches fail the generation fence (lease reclaim, §9)
  * stale ASSIGNED generations — withdrawn to the queue (the fence
    already blocked their dispatch; the row now says the truth)
  * orphaned bindings — assignments held by accounts that are no
    members return to the queue
  * ghost members — member rows whose account no longer exists retire
    (state-level: no membership seams)

Ambiguity (never auto-repaired): group state for a sync group that no
longer exists, members of such groups — surfaced for review.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

__all__ = ["FleetReconciler"]

LOG = logging.getLogger("libzent.fleet.reconciler")

AccountExists = Callable[[str], Awaitable[bool]]
GroupExists = Callable[[str], Awaitable[bool]]


class FleetReconciler:
    """Idempotent recovery over the FleetService projection."""

    def __init__(
        self,
        fleet: Any,
        *,
        account_exists: AccountExists | None = None,
        group_exists: GroupExists | None = None,
        interval_s: float = 30.0,
    ) -> None:
        self._fleet = fleet
        self._account_exists = account_exists
        self._group_exists = group_exists
        self._interval_s = float(interval_s)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ lifecycle

    async def reconcile(self) -> dict:
        """One pass; returns ``{checked, repaired, ambiguous}``. Every
        repair is a no-op on healthy state (run it twice, nothing moves
        the second time)."""
        fleet = self._fleet
        report: dict = {"checked": 0, "repaired": [], "ambiguous": []}
        bumped: set = set()

        # 1. crash recovery: RUNNING rows converge (suspend -> requeue)
        #    and the generation bump fences any zombie's stale dispatch
        for rec in fleet.assignments(status="running"):
            await fleet.suspend_task(rec.id, "recovery: running at reconcile")
            await fleet.resume_task(rec.id)
            if rec.group_name not in bumped:
                await fleet.bump_group_generation(rec.group_name)
                bumped.add(rec.group_name)
            report["repaired"].append(f"requeued running assignment {rec.id} ({rec.task_id})")
        for rec in fleet.assignments(status="suspended"):
            # suspend-recovery leftovers from an interrupted release
            if rec.account is not None and fleet.member(rec.account) is None:
                await fleet.resume_task(rec.id)
                report["repaired"].append(f"requeued orphaned-suspended {rec.id} ({rec.task_id})")

        # 2. stale ASSIGNED generations: the row must say the truth
        for rec in fleet.assignments(status="assigned"):
            state = fleet.group_state(rec.group_name)
            if state is not None and rec.generation != state.generation:
                await fleet.withdraw_task(rec.id, "recovery: stale generation")
                report["repaired"].append(f"withdrew stale-generation assignment {rec.id}")

        # 3. orphaned bindings: holders that are no longer members
        for rec in fleet.assignments(status="assigned"):
            if rec.account is not None and fleet.member(rec.account) is None:
                await fleet.withdraw_task(rec.id, "recovery: owner is not a member")
                report["repaired"].append(f"released orphaned assignment {rec.id}")

        # 4. ghost members: accounts that no longer exist
        if self._account_exists is not None:
            for member in list(fleet.members()):
                if not await self._account_exists(member.account):
                    await fleet.retire_member(member.account, "account removed")
                    report["repaired"].append(f"retired ghost member {member.account}")

        # 5. ghost groups: fleet state for a vanished sync group —
        #    AMBIGUOUS, never auto-deleted (data-loss call is human)
        if self._group_exists is not None:
            for state in list(fleet._groups.values()):
                if not await self._group_exists(state.group_name):
                    report["ambiguous"].append(
                        f"fleet state for unknown group {state.group_name!r}"
                    )

        report["checked"] = len(fleet.members()) + len(fleet.assignments())
        if report["repaired"]:
            LOG.info(
                "fleet reconcile repaired %d: %s",
                len(report["repaired"]),
                "; ".join(report["repaired"]),
            )
        if report["ambiguous"]:
            LOG.warning("fleet reconcile ambiguity: %s", "; ".join(report["ambiguous"]))
        return report

    def start(self) -> None:
        """Every-30s loop (spec §9). Idempotent; safe to call at boot."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()

        async def _loop() -> None:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    await self.reconcile()
                except Exception:
                    LOG.exception("periodic fleet reconcile failed")

        self._task = asyncio.create_task(_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
