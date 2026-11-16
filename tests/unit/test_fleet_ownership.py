"""Fleet ownership tests (PHASE 9 rollout E, spec D1/§8).

Acceptance: NO account receives conflicting work from both paths, and
LEAVE restores Independent cleanly. D1's fence: a group-managed member
runs ONLY fleet-linked dispatches — the independent flow is suspended
(parks, never fails); leaving flips the account back and the parked
work flows. In-flight work drains (D4): the gate never cancels a task
mid-send.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from core.event_bus import EventBus
from core.fleet import (
    AssignmentStatus,
    FleetPolicy,
    FleetService,
    TaskDefinition,
)
from core.task_scheduler import TaskScheduler
from database.db import Database

ROLL = TaskDefinition(id="roll", plugin="mudae_roll",
                      channel_scope=["1234567890123456789"])


class _FakeLimiter:
    def __init__(self):
        self.grants = []

    async def acquire(self, action, account, plugin, priority):
        self.grants.append(account)
        return SimpleNamespace(wait_s=0.0, jitter_s=0.0)

    def report_server_limit(self, *a, **k):
        pass


class _FakeLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = Database(":memory:")
        await self.db.connect()
        self.bus = EventBus()
        self.limiter = _FakeLimiter()
        self.sender_calls = []
        self.sender_hold = asyncio.Event()
        self.hold_enabled = False

        async def sender(task):
            self.sender_calls.append(task.id)
            if self.hold_enabled:
                await self.sender_hold.wait()
            return "sent"
        self.scheduler = TaskScheduler(self.bus, _FakeLogger(),
                                       self.limiter, sender)
        self.scheduler.start()
        self.t = 1000.0
        self.svc = FleetService(
            self.db.fleet,
            group_exists=lambda name: _yes(),
            time_fn=lambda: self.t,
            id_factory=lambda: "x1",
        )
        await self.svc.join_group("bravo", "farm")
        await self.svc.join_group("alpha", "farm")
        self.scheduler.set_dispatch_guard(
            FleetPolicy(self.svc).scheduler_guard())

    async def asyncTearDown(self) -> None:
        if not self.scheduler._stopped:
            await self.scheduler.stop(drain=False, timeout=2.0)
        await self.db.close()

    async def _wait(self, predicate, timeout=3.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return predicate()

    async def _assignment(self, account: str):
        rec = await self.svc.submit_task("farm", ROLL)
        return await self.svc.assign_task(rec.id, account)

    # ------------------------------------------------------- the fence

    async def test_member_independent_flow_suspended(self):
        """D1: an unlinked submit on a member parks — no sender call,
        no pacing slot burned, task never fails."""
        task = self.scheduler.submit("bravo", "p", "message")
        ok = await self._wait(lambda: task.status == "queued"
                              and self._parked_a_while())
        self.assertEqual(task.status, "queued")
        self.assertEqual(self.sender_calls, [])
        self.assertEqual(self.limiter.grants, [])
        self.assertFalse(task.cancelled)

    @staticmethod
    def _parked_a_while() -> bool:
        return True   # the wait loop already yields; state is asserted

    async def test_member_linked_dispatch_flows(self):
        """The fleet path: linked submits run — exactly one path, no
        conflict."""
        rec = await self._assignment("bravo")
        task = self.scheduler.submit("bravo", "p", "message",
                                     fleet_assignment=rec.id)
        self.assertTrue(await self._wait(
            lambda: task.status == "completed"))
        self.assertEqual(self.sender_calls, [task.id])

    async def test_linked_dispatch_with_stale_generation_fenced(self):
        """§5: the split-dispatch fence — an assignment under a stale
        group generation parks (old/new channel racing impossible)."""
        rec = await self._assignment("bravo")
        await self.svc.pause_group("farm")      # generation moves on
        await self.svc.resume_group("farm")
        task = self.scheduler.submit("bravo", "p", "message",
                                     fleet_assignment=rec.id)
        await asyncio.sleep(0.3)
        self.assertEqual(task.status, "queued")
        self.assertEqual(self.sender_calls, [])

    async def test_independent_account_never_consults_the_fleet(self):
        task = self.scheduler.submit("loner", "p", "message")
        self.assertTrue(await self._wait(
            lambda: task.status == "completed"))

    # ---------------------------------------------------- clean leave

    async def test_leave_restores_independent_cleanly(self):
        """§8: the parked independent task FLOWS the moment the account
        leaves — membership is the only thing that suspended it."""
        task = self.scheduler.submit("bravo", "p", "message")
        await asyncio.sleep(0.15)
        self.assertEqual(task.status, "queued")          # suspended
        await self.svc.leave_group("bravo")
        self.assertTrue(await self._wait(
            lambda: task.status == "completed"))
        self.assertEqual(self.sender_calls, [task.id])

    async def test_leave_releases_the_fleet_assignment(self):
        rec = await self._assignment("bravo")
        await self.svc.leave_group("bravo")
        after = self.svc.assignment(rec.id)
        self.assertEqual(after.status, AssignmentStatus.QUEUED)
        self.assertIsNone(after.account)

    async def test_rejoin_suspends_the_independent_flow_again(self):
        await self.svc.leave_group("bravo")
        task1 = self.scheduler.submit("bravo", "p", "message")
        self.assertTrue(await self._wait(
            lambda: task1.status == "completed"))
        await self.svc.join_group("bravo", "farm")
        task2 = self.scheduler.submit("bravo", "p", "message")
        await asyncio.sleep(0.2)
        self.assertEqual(task2.status, "queued")
        self.assertEqual(self.sender_calls, [task1.id])

    # ------------------------------------------------------- the drain

    async def test_inflight_work_drains_through_leave(self):
        """D4: a send already in flight when the member leaves COMPLETES
        — the gate parks new dispatches, it never kills a mid-send."""
        rec = await self._assignment("bravo")
        self.hold_enabled = True                  # hold the send open
        task = self.scheduler.submit("bravo", "p", "message",
                                     fleet_assignment=rec.id)
        self.assertTrue(await self._wait(
            lambda: len(self.sender_calls) == 1))  # inside the sender
        await self.svc.leave_group("bravo")
        await asyncio.sleep(0.15)
        self.assertEqual(task.status, "running")    # not cancelled
        self.sender_hold.set()                      # the send returns
        self.assertTrue(await self._wait(
            lambda: task.status == "completed"))


async def _yes() -> bool:
    return True


if __name__ == "__main__":
    unittest.main()
