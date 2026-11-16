"""Fleet recovery + regression tests (PHASE 9 rollout G, spec §9).

Acceptance: restart, disconnect, duplicate/missed events, concurrent
admin ops, stale dispatches — state CONVERGES without duplicate
execution. Every reconciler repair is idempotent (a healthy pass is a
no-op), auditable (transition history), and generation-fenced (a
zombie's stale dispatch cannot fire after recovery).
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from core.event_bus import EventBus
from core.fleet import (
    AssignmentStatus,
    FleetPolicy,
    FleetReconciler,
    FleetService,
    TaskDefinition,
)
from core.task_scheduler import TaskScheduler
from database.db import Database

ROLL = TaskDefinition(id="roll", plugin="mudae_roll",
                      channel_scope=["1234567890123456789"])


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = Database(":memory:")
        await self.db.connect()
        self.t = 1000.0
        self.known_accounts = {"bravo", "alpha"}
        self.known_groups = {"farm"}
        self.svc = FleetService(
            self.db.fleet,
            group_exists=lambda name: _in(name, self.known_groups),
            time_fn=lambda: self.t,
            id_factory=lambda: "x1",
        )
        await self.svc.join_group("bravo", "farm")
        await self.svc.join_group("alpha", "farm")
        self.reconciler = FleetReconciler(
            self.svc,
            account_exists=lambda name: _in(name, self.known_accounts),
            group_exists=lambda name: _in(name, self.known_groups),
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()


async def _in(name, collection) -> bool:
    return name in collection


class RestartRecoveryTests(_Base):
    async def test_running_at_reboot_requeues_and_fences(self):
        """THE restart case: the process died mid-run. Recovery requeues
        the work AND bumps the generation — a zombie's in-flight
        dispatch (still holding the old generation) is fenced."""
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await self.svc.report_started(rec.id)
        stale_generation = self.svc.assignment(rec.id).generation

        report = await self.reconciler.reconcile()
        self.assertTrue(any("requeued running" in r for r in report["repaired"]))
        after = self.svc.assignment(rec.id)
        self.assertEqual(after.status, AssignmentStatus.QUEUED)
        self.assertIsNone(after.account)
        self.assertGreater(
            self.svc.group_state("farm").generation, stale_generation)
        # the zombie's stale dispatch is rejected by the policy
        decision = await FleetPolicy(self.svc).evaluate(
            "bravo", assignment_id=rec.id)
        self.assertFalse(decision.allowed)

    async def test_reconcile_is_idempotent(self):
        """Duplicate/missed-event resilience: the second pass on healthy
        (or already-repaired) state changes NOTHING."""
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await self.svc.report_started(rec.id)
        first = await self.reconciler.reconcile()
        self.assertTrue(first["repaired"])
        second = await self.reconciler.reconcile()
        third = await self.reconciler.reconcile()
        self.assertEqual(second["repaired"], [])
        self.assertEqual(third["repaired"], [])

    async def test_healthy_state_pass_is_a_noop(self):
        report = await self.reconciler.reconcile()
        self.assertEqual(report["repaired"], [])
        self.assertEqual(report["ambiguous"], [])

    async def test_recovery_work_reassigns_and_dispatches(self):
        """After recovery the work continues: fresh assignment (new
        generation) passes the policy."""
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await self.svc.report_started(rec.id)
        await self.reconciler.reconcile()
        rec = await self.svc.assign_task(rec.id, "alpha")   # fresh stamp
        decision = await FleetPolicy(self.svc).evaluate(
            "alpha", assignment_id=rec.id)
        self.assertTrue(decision.allowed)


class DriftRepairTests(_Base):
    async def test_stale_generation_assigned_withdrawn(self):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await self.svc.pause_group("farm")       # generation moves on
        await self.svc.resume_group("farm")
        report = await self.reconciler.reconcile()
        self.assertTrue(any("stale-generation" in r for r in report["repaired"]))
        after = self.svc.assignment(rec.id)
        self.assertEqual(after.status, AssignmentStatus.QUEUED)

    async def test_orphaned_binding_released(self):
        """Membership moved outside the service (DB edit, restore gap):
        the reconciler returns the work to the queue."""
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await self.svc.leave_group("bravo")
        # drift: leave released it already; re-assign then DELETE the
        # member directly to simulate the outside-the-service path
        await self.svc.assign_task(rec.id, "alpha")
        await self.svc._repo.delete_member("alpha")
        await self.svc._reload()
        report = await self.reconciler.reconcile()
        self.assertTrue(any("released orphaned assignment" in r
                            for r in report["repaired"]))
        after = self.svc.assignment(rec.id)
        self.assertEqual(after.status, AssignmentStatus.QUEUED)
        self.assertIsNone(after.account)

    async def test_ghost_member_retired(self):
        """An account removed while managed: the member row retires and
        its work releases — state-level, no membership seams."""
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        self.known_accounts.discard("bravo")       # the account vanished
        report = await self.reconciler.reconcile()
        self.assertTrue(any("ghost member bravo" in r
                            for r in report["repaired"]))
        self.assertIsNone(self.svc.member("bravo"))
        self.assertEqual(self.svc.assignment(rec.id).status,
                         AssignmentStatus.QUEUED)

    async def test_ghost_group_surfaced_not_deleted(self):
        """Ambiguity is surfaced for review — never auto-deleted."""
        await self.svc.pause_group("farm")
        self.known_groups.discard("farm")
        report = await self.reconciler.reconcile()
        self.assertTrue(any("unknown group 'farm'" in a
                            for a in report["ambiguous"]))
        self.assertIsNotNone(self.svc.group_state("farm"))   # kept


class ConcurrencyTests(_Base):
    async def test_concurrent_admin_ops_and_reconcile_converge(self):
        """Concurrent operator commands + the reconciler race: the
        service lock serializes every move; the end state is valid and
        no repair corrupts an in-flight command."""
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await asyncio.gather(
            self.svc.pause_group("farm"),
            self.svc.pause_member("alpha"),
            self.reconciler.reconcile(),
            self.svc.resume_member("alpha"),
            return_exceptions=True,
        )
        # whatever interleaving ran, the projection matches the DB
        fresh = FleetService(self.db.fleet, time_fn=lambda: self.t)
        await fresh.load()
        self.assertEqual(
            sorted(m.account for m in fresh.members()),
            ["alpha", "bravo"])
        self.assertEqual(
            fresh.assignment(rec.id).status,
            self.svc.assignment(rec.id).status)

    async def test_zombie_dispatch_after_recovery_never_reaches_sender(self):
        """End-to-end G acceptance: recovery + a stale linked dispatch —
        the fence keeps the zombie task parked, forever."""
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await self.svc.report_started(rec.id)
        stale_gen = self.svc.assignment(rec.id).generation
        await self.reconciler.reconcile()

        calls = []

        async def sender(task):
            calls.append(task.id)
            return "sent"

        class _Limiter:
            async def acquire(self, *a):
                return SimpleNamespace(wait_s=0.0, jitter_s=0.0)

        class _Log:
            def info(self, *a, **k):
                pass

            def warning(self, *a, **k):
                pass

        scheduler = TaskScheduler(EventBus(), _Log(), _Limiter(), sender)
        scheduler.start()
        try:
            scheduler.set_dispatch_guard(
                FleetPolicy(self.svc).scheduler_guard())
            # the zombie submits with its OLD assignment stamp: the
            # assignment is queued (not assigned) AND the generation is
            # stale — double-fenced
            task = scheduler.submit("bravo", "p", "message",
                                    fleet_assignment=rec.id)
            await asyncio.sleep(0.3)
            self.assertEqual(task.status, "queued")
            self.assertEqual(calls, [])
            # the recovered path works: fresh assignment dispatches
            await self.svc.resume_group("farm") if self.svc.group_state(
                "farm").paused else None
            rec2 = await self.svc.assign_task(rec.id, "alpha")
            task2 = scheduler.submit("alpha", "p", "message",
                                     fleet_assignment=rec2.id)
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                if task2.status == "completed":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(task2.status, "completed")
            self.assertEqual(calls, [task2.id])   # exactly one send
        finally:
            await scheduler.stop(drain=False, timeout=2.0)


if __name__ == "__main__":
    unittest.main()
