"""FleetService tests (PHASE 9 rollout B).

Every command through the ONE service, against a real in-memory
SQLite database (the repo is exercised, not stubbed): transition
guards, handoff atomicity, generation fencing, cooldown rules,
write-ahead history, and the projection round-trip (D2 — the DB is
authoritative, a fresh service must land on the same state).
"""
from __future__ import annotations

import unittest

from core.fleet import (
    AssignmentStatus,
    CooldownScopeType,
    CooldownSource,
    ExecutionMode,
    FleetEvent,
    FleetService,
    MemberEligibility,
    TaskDefinition,
    effective_cooldown,
)
from database.db import Database

ROLL = TaskDefinition(id="roll", plugin="mudae_roll",
                      channel_scope=["1234567890123456789"])


class FleetServiceTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = Database(":memory:")
        await self.db.connect()
        self.events: list = []
        self.joins: list = []
        self.leaves: list = []
        self.known_groups = {"farm"}
        self.t = 1000.0
        self._next_id = iter(range(1, 1000))
        self.svc = FleetService(
            self.db.fleet,
            group_exists=lambda name: self._ok(name in self.known_groups),
            membership_join=self._mj,
            membership_leave=self._ml,
            publisher=self.events.append,
            time_fn=lambda: self.t,
            id_factory=lambda: f"a{next(self._next_id)}",
        )
        await self.svc.join_group("bravo", "farm")
        await self.svc.join_group("alpha", "farm")
        self.joins.clear()
        self.leaves.clear()
        self.events.clear()

    async def asyncTearDown(self) -> None:
        await self.db.close()

    # seam stubs (async per the service contracts)
    async def _ok(self, value: bool) -> bool:
        return value

    async def _mj(self, account: str, group: str) -> None:
        self.joins.append((account, group))

    async def _ml(self, account: str) -> None:
        self.leaves.append(account)


class JoinLeaveTests(FleetServiceTestBase):
    async def test_join_creates_eligible_group_managed_member(self):
        member = self.svc.member("bravo")
        self.assertEqual(member.mode, ExecutionMode.GROUP_MANAGED)
        self.assertEqual(member.eligibility, MemberEligibility.ELIGIBLE)
        self.assertEqual(member.group_name, "farm")

    async def test_join_records_history_and_publishes(self):
        await self.svc.leave_group("alpha")       # clear setup noise
        self.events.clear()
        await self.svc.join_group("alpha", "farm")
        self.assertTrue(any(e.kind == "MemberJoined" for e in self.events))
        rows = await self.svc.transitions(group_name="farm")
        self.assertTrue(any(r.kind == "MemberJoined" and r.account == "alpha"
                            for r in rows))
        event = next(e for e in self.events if e.kind == "MemberJoined")
        self.assertEqual(event.group_id, "farm")
        self.assertGreaterEqual(event.generation, 2)
        self.assertTrue(event.correlation_id)

    async def test_join_unknown_group_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            await self.svc.join_group("gamma", "nope")
        self.assertIn("unknown sync group", str(ctx.exception))

    async def test_double_join_rejected_single_group_locked(self):
        with self.assertRaises(ValueError) as ctx:
            await self.svc.join_group("bravo", "farm")
        self.assertIn("leave first", str(ctx.exception))

    async def test_join_bumps_generation(self):
        before = self.svc.group_state("farm").generation
        await self.svc.leave_group("alpha")
        await self.svc.join_group("alpha", "farm")
        after = self.svc.group_state("farm").generation
        self.assertEqual(after, before + 2)   # leave bumped, join bumped

    async def test_leave_releases_work_and_restores_independent(self):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await self.svc.leave_group("bravo")
        self.assertIsNone(self.svc.member("bravo"))
        self.assertEqual(self.leaves, ["bravo"])
        after = self.svc.assignment(rec.id)
        self.assertEqual(after.status, AssignmentStatus.QUEUED)   # reassigned
        self.assertIsNone(after.account)

    async def test_leave_suspends_running_work(self):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await self.svc.report_started(rec.id)
        await self.svc.leave_group("bravo")
        after = self.svc.assignment(rec.id)
        self.assertEqual(after.status, AssignmentStatus.SUSPENDED)
        self.assertIn("leaving", after.error)

    async def test_projection_round_trips_from_the_database(self):
        """D2: the DB is authoritative — a FRESH service must land on
        the exact same state."""
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "alpha")
        fresh = FleetService(self.db.fleet, publisher=None)
        loaded = await fresh.load()
        self.assertEqual(loaded, {"groups": 1, "members": 2,
                                  "assignments": 1})
        self.assertEqual(fresh.member("bravo").eligibility,
                         MemberEligibility.ELIGIBLE)
        self.assertEqual(fresh.assignment(rec.id).status,
                         AssignmentStatus.ASSIGNED)


class GroupPauseTests(FleetServiceTestBase):
    async def test_pause_sets_flag_bumps_generation_and_gates_assign(self):
        before = self.svc.group_state("farm").generation
        await self.svc.pause_group("farm")
        state = self.svc.group_state("farm")
        self.assertTrue(state.paused)
        self.assertEqual(state.generation, before + 1)
        rec = await self.svc.submit_task("farm", ROLL)
        with self.assertRaises(ValueError) as ctx:
            await self.svc.assign_task(rec.id, "bravo")
        self.assertIn("paused", str(ctx.exception))

    async def test_double_pause_rejected(self):
        await self.svc.pause_group("farm")
        with self.assertRaises(ValueError):
            await self.svc.pause_group("farm")

    async def test_resume_requires_paused(self):
        with self.assertRaises(ValueError):
            await self.svc.resume_group("farm")
        await self.svc.pause_group("farm")
        await self.svc.resume_group("farm")
        self.assertFalse(self.svc.group_state("farm").paused)

    async def test_join_while_paused_keeps_the_pause_flag(self):
        await self.svc.pause_group("farm")
        await self.svc.leave_group("alpha")
        await self.svc.join_group("alpha", "farm")
        self.assertTrue(self.svc.group_state("farm").paused)


class MemberPauseTests(FleetServiceTestBase):
    async def test_pause_member_blocks_assignment(self):
        await self.svc.pause_member("bravo")
        rec = await self.svc.submit_task("farm", ROLL)
        with self.assertRaises(ValueError) as ctx:
            await self.svc.assign_task(rec.id, "bravo")
        self.assertIn("paused", str(ctx.exception))

    async def test_resume_returns_to_eligible(self):
        await self.svc.pause_member("bravo")
        await self.svc.resume_member("bravo")
        self.assertEqual(self.svc.member("bravo").eligibility,
                         MemberEligibility.ELIGIBLE)

    async def test_resume_requires_paused(self):
        with self.assertRaises(ValueError):
            await self.svc.resume_member("bravo")


class AssignmentLifecycleTests(FleetServiceTestBase):
    async def test_full_lifecycle(self):
        rec = await self.svc.submit_task("farm", ROLL)
        self.assertEqual(rec.status, AssignmentStatus.QUEUED)
        rec = await self.svc.assign_task(rec.id, "bravo")
        self.assertEqual(rec.account, "bravo")
        rec = await self.svc.report_started(rec.id)
        self.assertEqual(rec.status, AssignmentStatus.RUNNING)
        self.assertIsNotNone(rec.started_at)
        rec = await self.svc.report_completed(rec.id)
        self.assertEqual(rec.status, AssignmentStatus.COMPLETED)
        self.assertIsNotNone(rec.stopped_at)
        self.assertGreater(rec.version, 1)          # optimistic bumps

    async def test_assign_stamps_live_generation(self):
        await self.svc.pause_group("farm")
        await self.svc.resume_group("farm")          # generation += 2
        rec = await self.svc.submit_task("farm", ROLL)
        rec = await self.svc.assign_task(rec.id, "bravo")
        self.assertEqual(rec.generation,
                         self.svc.group_state("farm").generation)

    async def test_assign_rejects_wrong_group_member_and_unknown(self):
        rec = await self.svc.submit_task("farm", ROLL)
        with self.assertRaises(ValueError):
            await self.svc.assign_task(rec.id, "ghost")
        with self.assertRaises(ValueError):
            await self.svc.assign_task("nope", "bravo")

    async def test_failed_stays_failed_without_retry_allowance(self):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await self.svc.report_started(rec.id)
        rec = await self.svc.report_failed(rec.id, "wire died")
        self.assertEqual(rec.status, AssignmentStatus.FAILED)
        self.assertEqual(rec.error, "wire died")

    async def test_failed_requeues_under_explicit_retry(self):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await self.svc.report_started(rec.id)
        rec = await self.svc.report_failed(rec.id, "wire died",
                                           retry_allowed=True)
        self.assertEqual(rec.status, AssignmentStatus.QUEUED)
        kinds = [e.kind for e in self.events]
        self.assertIn("TaskFailed", kinds)
        self.assertIn("TaskRequeued", kinds)

    async def test_withdraw_returns_task_to_queue(self):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        rec = await self.svc.withdraw_task(rec.id)
        self.assertEqual(rec.status, AssignmentStatus.QUEUED)
        self.assertIsNone(rec.account)

    async def test_suspend_resume_cancel_paths(self):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        rec = await self.svc.suspend_task(rec.id, "operator hold")
        self.assertEqual(rec.status, AssignmentStatus.SUSPENDED)
        rec = await self.svc.resume_task(rec.id)
        self.assertEqual(rec.status, AssignmentStatus.QUEUED)
        rec = await self.svc.cancel_task(rec.id)
        self.assertEqual(rec.status, AssignmentStatus.CANCELLED)
        with self.assertRaises(ValueError):
            await self.svc.resume_task(rec.id)      # terminal stays terminal

    async def test_illegal_move_carries_the_phase_a_reason(self):
        rec = await self.svc.submit_task("farm", ROLL)
        with self.assertRaises(ValueError) as ctx:
            await self.svc.report_started(rec.id)   # QUEUED -> RUNNING (skip)
        self.assertIn("cannot move", str(ctx.exception))


class HandoffTests(FleetServiceTestBase):
    async def test_handoff_moves_ownership_and_keeps_task_identity(self):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        dest = await self.svc.handoff(rec.id, "alpha")
        self.assertEqual(dest.task_id, ROLL.id)
        self.assertEqual(dest.account, "alpha")
        self.assertEqual(dest.status, AssignmentStatus.ASSIGNED)
        source = self.svc.assignment(rec.id)
        self.assertEqual(source.status, AssignmentStatus.CANCELLED)
        self.assertIn("handoff to alpha", source.error)
        self.assertTrue(any(e.kind == "AssignmentHandoff" for e in self.events))

    async def test_failed_destination_check_never_destroys_the_source(self):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        with self.assertRaises(ValueError):
            await self.svc.handoff(rec.id, "ghost")          # not a member
        source = self.svc.assignment(rec.id)
        self.assertEqual(source.status, AssignmentStatus.ASSIGNED)
        self.assertEqual(source.account, "bravo")

    async def test_running_handoff_rejected_until_drain_machinery(self):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await self.svc.report_started(rec.id)
        with self.assertRaises(ValueError) as ctx:
            await self.svc.handoff(rec.id, "alpha")
        self.assertIn("rollout E", str(ctx.exception))

    async def test_handoff_to_paused_destination_rejected(self):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, "bravo")
        await self.svc.pause_member("alpha")
        with self.assertRaises(ValueError):
            await self.svc.handoff(rec.id, "alpha")
        self.assertEqual(self.svc.assignment(rec.id).status,
                         AssignmentStatus.ASSIGNED)


class CooldownTests(FleetServiceTestBase):
    async def test_apply_and_effective_cooldown(self):
        cd = await self.svc.apply_cooldown(
            CooldownScopeType.TASK, "roll", "observed limit", 60.0)
        self.assertEqual(cd.source, CooldownSource.MANUAL)
        active = await self.svc.policy_cooldowns()
        self.assertEqual(len(active), 1)
        self.assertIs(effective_cooldown(active, now=self.t), active[0])

    async def test_manual_clears_observed_never(self):
        manual = await self.svc.apply_cooldown(
            CooldownScopeType.GROUP, "farm", "operator hold", 30.0)
        observed = await self.svc.apply_cooldown(
            CooldownScopeType.TASK, "roll", "limit marker", 60.0,
            source=CooldownSource.OBSERVED)
        cleared = await self.svc.clear_cooldown(manual.id)
        self.assertIsNotNone(cleared.cleared_at)
        with self.assertRaises(ValueError) as ctx:
            await self.svc.clear_cooldown(observed.id)
        self.assertIn("only manual", str(ctx.exception))
        self.assertEqual(
            len(await self.svc.cooldowns(active_only=True)), 1)

    async def test_double_clear_rejected(self):
        cd = await self.svc.apply_cooldown(
            CooldownScopeType.ACCOUNT, "bravo", "hold", 30.0)
        await self.svc.clear_cooldown(cd.id)
        with self.assertRaises(ValueError):
            await self.svc.clear_cooldown(cd.id)

    async def test_bad_shapes_rejected(self):
        with self.assertRaises(ValueError):
            await self.svc.apply_cooldown(
                "guild", "x", "r", 30.0)                      # bad scope
        with self.assertRaises(ValueError):
            await self.svc.apply_cooldown(
                CooldownScopeType.TASK, "roll", "r", -5.0)    # bad duration
        with self.assertRaises(ValueError):
            await self.svc.clear_cooldown(999)                 # unknown id


class WriteAheadTests(FleetServiceTestBase):
    async def test_publishing_failure_never_breaks_the_transition(self):
        """spec §3: delivery is an optimization — persisted first."""
        def boom(event):
            raise RuntimeError("bus on fire")
        await self.svc.leave_group("alpha")
        self.events.clear()
        svc = FleetService(self.db.fleet, publisher=boom,
                           time_fn=lambda: self.t)
        await svc.load()
        await svc.pause_group("farm")          # must not raise
        self.assertTrue(svc.group_state("farm").paused)
        rows = await svc.transitions(group_name="farm")
        self.assertTrue(any(r.kind == "GroupPaused" for r in rows))

    async def test_history_is_append_only_with_correlation_ids(self):
        await self.svc.pause_group("farm")
        rows = await self.svc.transitions(group_name="farm")
        self.assertGreaterEqual(len(rows), 1)
        for r in rows:
            self.assertTrue(r.correlation_id)
            self.assertGreater(r.occurred_at, 0)


if __name__ == "__main__":
    unittest.main()
