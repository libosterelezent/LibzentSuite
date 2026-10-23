"""FleetPolicy + scheduler-gate tests (PHASE 9 rollout C, spec §4).

Acceptance under test: a PAUSED GROUP cannot start new work, and
policy changes NEVER bypass pacing (the limiter grant still gates
every dispatch after the guard). Plus the check-order contract,
cooldown retry_at propagation, generation fencing, and D1's boundary —
non-members flow untouched.
"""
from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

from core.event_bus import EventBus
from core.fleet import (
    CooldownScopeType,
    FleetPolicy,
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
        self.svc = FleetService(
            self.db.fleet,
            group_exists=lambda name: self._yes(),
            time_fn=lambda: self.t,
            id_factory=lambda: "x1",
        )
        await self.svc.join_group("bravo", "farm")
        await self.svc.join_group("alpha", "farm")

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def _yes(self) -> bool:
        return True


class PolicyCheckOrderTests(_Base):
    async def test_non_member_flows_untouched(self):
        """D1 boundary: independent accounts are none of the engine's
        business — allowed with the not-managed reason."""
        decision = await FleetPolicy(self.svc).evaluate("loner")
        self.assertTrue(decision.allowed)
        self.assertIn("not fleet-managed", decision.reason)

    async def test_eligible_member_with_assignment_allowed(self):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        decision = await FleetPolicy(self.svc).evaluate(
            "bravo", assignment_id=rec.id)
        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.retry_at)

    async def test_member_unlinked_flow_suspended(self):
        """D1: group-managed members run only fleet-linked dispatches —
        an independent-flow submit is suspended (parks)."""
        decision = await FleetPolicy(self.svc).evaluate("bravo")
        self.assertFalse(decision.allowed)
        self.assertIn("independent flow suspended", decision.reason)

    async def test_paused_group_blocks(self):
        await self.svc.pause_group("farm")
        decision = await FleetPolicy(self.svc).evaluate("bravo")
        self.assertFalse(decision.allowed)
        self.assertIn("paused", decision.reason)

    async def test_pause_beats_cooldown_in_check_order(self):
        """§4 order: group checks run BEFORE cooldowns — the reason must
        name the pause, not the cooldown."""
        await self.svc.apply_cooldown(CooldownScopeType.ACCOUNT, "bravo",
                                      "manual hold", 60.0)
        await self.svc.pause_group("farm")
        decision = await FleetPolicy(self.svc).evaluate("bravo")
        self.assertFalse(decision.allowed)
        self.assertIn("paused", decision.reason)

    async def test_disabled_group_blocks(self):
        async def disabled(name: str) -> bool:
            return False
        decision = await FleetPolicy(
            self.svc, group_enabled=disabled).evaluate("bravo")
        self.assertFalse(decision.allowed)
        self.assertIn("disabled", decision.reason)

    async def test_disconnected_account_blocks(self):
        async def offline(name: str) -> bool:
            return False
        decision = await FleetPolicy(
            self.svc, account_connected=offline).evaluate("bravo")
        self.assertFalse(decision.allowed)
        self.assertIn("not connected", decision.reason)

    async def test_paused_member_blocks_with_state_reason(self):
        await self.svc.pause_member("bravo")
        decision = await FleetPolicy(self.svc).evaluate("bravo")
        self.assertFalse(decision.allowed)
        self.assertIn("paused", decision.reason)


class PolicyCooldownTests(_Base):
    async def test_group_cooldown_blocks_with_retry_at(self):
        await self.svc.apply_cooldown(CooldownScopeType.GROUP, "farm",
                                      "operator pause-window", 120.0)
        decision = await FleetPolicy(self.svc).evaluate("bravo")
        self.assertFalse(decision.allowed)
        self.assertIn("cooldown", decision.reason)
        self.assertEqual(decision.retry_at, self.t + 120.0)

    async def test_most_restrictive_scope_wins(self):
        """Account-scope (60s) vs group-scope (300s): the later expiry
        is the verdict's retry_at."""
        await self.svc.apply_cooldown(CooldownScopeType.ACCOUNT, "bravo",
                                      "short", 60.0)
        await self.svc.apply_cooldown(CooldownScopeType.GROUP, "farm",
                                      "long", 300.0)
        decision = await FleetPolicy(self.svc).evaluate("bravo")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.retry_at, self.t + 300.0)

    async def test_cleared_manual_cooldown_stops_blocking(self):
        cd = await self.svc.apply_cooldown(CooldownScopeType.ACCOUNT,
                                           "bravo", "hold", 60.0)
        await self.svc.clear_cooldown(cd.id)
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        decision = await FleetPolicy(self.svc).evaluate(
            "bravo", assignment_id=rec.id)
        self.assertTrue(decision.allowed)


class PolicyAssignmentTests(_Base):
    async def _assigned(self, account: str = "bravo"):
        rec = await self.svc.submit_task("farm", ROLL)
        return await self.svc.assign_task(rec.id, account)

    async def test_valid_assignment_allowed(self):
        rec = await self._assigned()
        decision = await FleetPolicy(self.svc).evaluate(
            "bravo", assignment_id=rec.id)
        self.assertTrue(decision.allowed)

    async def test_unknown_assignment_rejected(self):
        decision = await FleetPolicy(self.svc).evaluate(
            "bravo", assignment_id="nope")
        self.assertFalse(decision.allowed)
        self.assertIn("unknown fleet assignment", decision.reason)

    async def test_assignment_held_by_other_rejected(self):
        rec = await self._assigned("alpha")
        decision = await FleetPolicy(self.svc).evaluate(
            "bravo", assignment_id=rec.id)
        self.assertFalse(decision.allowed)
        self.assertIn("held by", decision.reason)

    async def test_generation_mismatch_fences_stale_dispatch(self):
        """spec §5: assigned under generation N, live generation N+1 —
        the dispatch is rejected (split-dispatch fencing)."""
        rec = await self._assigned()
        await self.svc.pause_group("farm")     # bumps the generation
        await self.svc.resume_group("farm")
        decision = await FleetPolicy(self.svc).evaluate(
            "bravo", assignment_id=rec.id)
        self.assertFalse(decision.allowed)
        self.assertIn("stale dispatch fenced", decision.reason)

    async def test_non_assigned_status_rejected(self):
        rec = await self._assigned()
        await self.svc.report_started(rec.id)   # RUNNING now
        decision = await FleetPolicy(self.svc).evaluate(
            "bravo", assignment_id=rec.id)
        self.assertFalse(decision.allowed)
        self.assertIn("running", decision.reason)


# ------------------------------------------------------------- scheduler

class _FakeLimiter:
    async def acquire(self, action, account, plugin, priority):
        return SimpleNamespace(wait_s=0.0, jitter_s=0.0)

    def report_server_limit(self, *a, **k):
        pass


class _FakeSender:
    def __init__(self):
        self.calls = []

    async def __call__(self, task):
        self.calls.append(task.id)
        return "sent"


class _FakeLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class SchedulerGateTests(unittest.IsolatedAsyncioTestCase):
    """The guard bridge on the REAL scheduler: parks, revalidates after
    the grant, fails open on guard errors — rollout C's acceptance."""

    async def asyncSetUp(self) -> None:
        self.bus = EventBus()
        self.sender = _FakeSender()
        self.scheduler = TaskScheduler(
            self.bus, _FakeLogger(), _FakeLimiter(), self.sender)
        self.scheduler.start()

    async def asyncTearDown(self) -> None:
        if not self.scheduler._stopped:
            await self.scheduler.stop(drain=False, timeout=2.0)

    async def _wait_status(self, task, status, timeout=3.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if task.status == status:
                return True
            await asyncio.sleep(0.01)
        return task.status == status

    async def test_disallowed_task_parks_then_dispatches(self):
        gate = {"open": False}

        async def guard(task):
            return SimpleNamespace(
                allowed=gate["open"],
                retry_at=time.time() + 0.1 if not gate["open"] else None)
        self.scheduler.set_dispatch_guard(guard)
        task = self.scheduler.submit("alpha", "p", "message")
        await asyncio.sleep(0.25)
        self.assertEqual(task.status, "queued")     # parked, not failed
        self.assertEqual(self.sender.calls, [])     # never dispatched
        gate["open"] = True                          # operator resumes
        self.assertTrue(await self._wait_status(task, "completed"))

    async def test_disallow_after_grant_reparks_without_sending(self):
        """Revalidation after limiter.acquire (spec §4): the group
        paused mid-queue — the task re-parks, the sender never runs."""
        calls = {"n": 0}

        async def guard(task):
            calls["n"] += 1
            # allow the pre-dispatch check, reject the post-grant one
            return SimpleNamespace(allowed=calls["n"] >= 3, retry_at=None)
        self.scheduler.set_dispatch_guard(guard)
        task = self.scheduler.submit("alpha", "p", "message")
        # 1st allow -> grant -> 2nd (post-grant) reject -> park -> 3rd allow
        self.assertTrue(await self._wait_status(task, "completed"))
        self.assertEqual(self.sender.calls, [task.id])
        self.assertGreaterEqual(calls["n"], 3)

    async def test_guard_error_fails_open(self):
        async def boom(task):
            raise RuntimeError("policy engine on fire")
        self.scheduler.set_dispatch_guard(boom)
        task = self.scheduler.submit("alpha", "p", "message")
        self.assertTrue(await self._wait_status(task, "completed"))

    async def test_cancel_while_parked(self):
        async def guard(task):
            return SimpleNamespace(allowed=False, retry_at=None)
        self.scheduler.set_dispatch_guard(guard)
        task = self.scheduler.submit("alpha", "p", "message")
        await asyncio.sleep(0.1)
        self.assertEqual(task.status, "queued")
        self.assertTrue(self.scheduler.cancel(task.id))
        self.assertTrue(await self._wait_status(task, "cancelled"))
        self.assertEqual(self.sender.calls, [])


class PausedGroupAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """Rollout C acceptance, end to end: FleetService + FleetPolicy +
    the real TaskScheduler. A paused group cannot start new work;
    pacing is never bypassed (every dispatch still acquires a grant)."""

    async def asyncSetUp(self) -> None:
        self.db = Database(":memory:")
        await self.db.connect()
        self.bus = EventBus()
        self.sender = _FakeSender()
        self.limiter = _FakeLimiter()
        self.grants = []
        orig_acquire = self.limiter.acquire

        async def counting_acquire(action, account, plugin, priority):
            grant = await orig_acquire(action, account, plugin, priority)
            self.grants.append(account)
            return grant
        self.limiter.acquire = counting_acquire

        self.scheduler = TaskScheduler(
            self.bus, _FakeLogger(), self.limiter, self.sender)
        self.scheduler.start()
        self.svc = FleetService(
            self.db.fleet,
            group_exists=lambda name: _true(),
            time_fn=lambda: 1000.0,
            id_factory=lambda: "x1",
        )
        await self.svc.join_group("bravo", "farm")
        self.scheduler.set_dispatch_guard(
            FleetPolicy(self.svc).scheduler_guard())

    async def asyncTearDown(self) -> None:
        if not self.scheduler._stopped:
            await self.scheduler.stop(drain=False, timeout=2.0)
        await self.db.close()

    async def test_paused_group_cannot_start_new_work_until_resumed(self):
        await self.svc.pause_group("farm")
        rec = await self.svc.submit_task("farm", ROLL)
        with self.assertRaises(ValueError):
            # paused group: no assignment, and the linked dispatch below
            # would park on the same verdict
            await self.svc.assign_task(rec.id, "bravo")

        await self.svc.resume_group("farm")
        await self.svc.assign_task(rec.id, "bravo")   # fresh generation
        task = self.scheduler.submit("bravo", "p", "message",
                                     fleet_assignment=rec.id)
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            if task.status == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(task.status, "completed")
        self.assertEqual(self.sender.calls, [task.id])
        self.assertEqual(self.grants, ["bravo"])   # pacing still applied

    async def test_independent_account_unaffected_by_group_pause(self):
        """D1: membership decides the fleet's reach — a non-member's
        dispatch never consults group state."""
        await self.svc.pause_group("farm")
        task = self.scheduler.submit("loner", "p", "message")
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            if task.status == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(task.status, "completed")


async def _true() -> bool:
    return True


if __name__ == "__main__":
    unittest.main()
