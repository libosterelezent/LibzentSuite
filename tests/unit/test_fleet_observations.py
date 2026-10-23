"""Fleet observations tests (PHASE 9 rollout D, spec §2/§3/D3).

Rollout D's acceptance is a SHAPE property: parser output alone cannot
bypass state/policy. These tests pin it from every side — observations
only ever ADD restrictions (cooldowns, WAITING), move work through the
transition tables (relay), and never produce a dispatch or an ALLOWED
verdict; the policy still gates everything afterwards.
"""
from __future__ import annotations

import unittest

from core.fleet import (
    AssignmentStatus,
    CooldownScopeType,
    CooldownSource,
    FleetObservations,
    FleetPolicy,
    FleetService,
    FleetTaskHandler,
    MemberEligibility,
    Observation,
    TaskDefinition,
    definitions_of,
    handler_of,
)
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
            group_exists=lambda name: _yes(),
            time_fn=lambda: self.t,
            id_factory=lambda: "x1",
        )
        await self.svc.join_group("bravo", "farm")
        await self.svc.join_group("alpha", "farm")
        self.intake = FleetObservations(self.svc, time_fn=lambda: self.t)

    async def asyncTearDown(self) -> None:
        await self.db.close()


async def _yes() -> bool:
    return True


class DedupTests(_Base):
    async def test_same_marker_twice_registers_once(self):
        first = await self.intake.observe(Observation(
            source_account="bravo", observation_type="limit_marker",
            message_id="999", cooldown_window_s=60.0, reason="game limit"))
        second = await self.intake.observe(Observation(
            source_account="bravo", observation_type="limit_marker",
            message_id="999", cooldown_window_s=60.0, reason="game limit"))
        self.assertTrue(first)
        self.assertFalse(second)       # the replay is a no-op
        active = await self.svc.cooldowns(active_only=True)
        self.assertEqual(len(active), 1)

    async def test_different_message_ids_are_distinct(self):
        for mid in ("999", "1000"):
            handled = await self.intake.observe(Observation(
                source_account="bravo",
                observation_type="limit_marker",
                message_id=mid, cooldown_window_s=60.0, reason="r"))
            self.assertTrue(handled)

    async def test_unknown_type_changes_nothing(self):
        handled = await self.intake.observe(Observation(
            source_account="bravo", observation_type="vibe_check",
            message_id="1"))
        self.assertFalse(handled)
        self.assertEqual(await self.svc.cooldowns(active_only=True), [])
        self.assertEqual(self.svc.member("bravo").eligibility,
                         MemberEligibility.ELIGIBLE)

    async def test_non_member_report_ignored(self):
        """D1: the fleet is not the receiving account's business."""
        handled = await self.intake.observe(Observation(
            source_account="loner", observation_type="limit_marker",
            message_id="1", cooldown_window_s=60.0))
        self.assertFalse(handled)


class LimitObservationTests(_Base):
    async def test_limit_registers_observed_cooldown_with_window(self):
        await self.intake.observe(Observation(
            source_account="bravo", observation_type="limit_marker",
            message_id="9", cooldown_window_s=120.0, reason="game limit"))
        active = await self.svc.cooldowns(active_only=True)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].source, CooldownSource.OBSERVED)
        self.assertEqual(active[0].expires_at, self.t + 120.0)
        # the game's own window is never operator-clearable
        with self.assertRaises(ValueError):
            await self.svc.clear_cooldown(active[0].id)

    async def test_limit_without_window_never_invents_one(self):
        await self.intake.observe(Observation(
            source_account="bravo", observation_type="limit_marker",
            message_id="9", cooldown_window_s=0))
        self.assertEqual(await self.svc.cooldowns(active_only=True), [])

    async def test_server_cooldown_source_is_server(self):
        await self.intake.observe(Observation(
            source_account="bravo", observation_type="server_cooldown",
            message_id="429", cooldown_window_s=30.0, reason="429"))
        active = await self.svc.cooldowns(active_only=True)
        self.assertEqual(active[0].source, CooldownSource.SERVER)


class RelayTests(_Base):
    async def _running(self, account: str):
        rec = await self.svc.submit_task("farm", ROLL)
        await self.svc.assign_task(rec.id, account)
        await self.svc.report_started(rec.id)
        return rec

    async def test_limit_relays_running_work_to_next_member(self):
        """D3 relay: suspend -> scoped cooldown -> reassign to the next
        ELIGIBLE member; the task keeps its identity (same row)."""
        rec = await self._running("bravo")
        await self.intake.observe(Observation(
            source_account="bravo", observation_type="limit_marker",
            message_id="9", cooldown_window_s=300.0, reason="game limit"))
        after = self.svc.assignment(rec.id)
        self.assertEqual(after.account, "alpha")        # relayed
        self.assertEqual(after.status, AssignmentStatus.ASSIGNED)
        # the cooling account carries the observed cooldown
        decision = await FleetPolicy(self.svc).evaluate("bravo")
        self.assertFalse(decision.allowed)
        self.assertIn("cooldown", decision.reason)
        self.assertEqual(decision.retry_at, self.t + 300.0)

    async def test_relay_skips_ineligible_candidates(self):
        await self.svc.pause_member("alpha")     # the only other member
        rec = await self._running("bravo")
        await self.intake.observe(Observation(
            source_account="bravo", observation_type="limit_marker",
            message_id="9", cooldown_window_s=60.0, reason="r"))
        after = self.svc.assignment(rec.id)
        self.assertIsNone(after.account)               # stays queued
        self.assertEqual(after.status, AssignmentStatus.QUEUED)

    async def test_relay_never_lands_on_the_cooling_member(self):
        """Parser output cannot force work: even with no other member,
        the assignment releases rather than dispatching to the account
        the observation just restricted."""
        rec = await self._running("bravo")
        await self.svc.leave_group("alpha")
        await self.intake.observe(Observation(
            source_account="bravo", observation_type="limit_marker",
            message_id="9", cooldown_window_s=60.0, reason="r"))
        after = self.svc.assignment(rec.id)
        self.assertNotEqual(after.account, "bravo")
        self.assertEqual(after.status, AssignmentStatus.QUEUED)


class ConnectivityTests(_Base):
    async def test_disconnect_moves_member_to_waiting(self):
        await self.intake.observe(Observation(
            source_account="bravo",
            observation_type="account_disconnected",
            message_id="dc@1", observed_at=1.0))
        self.assertEqual(self.svc.member("bravo").eligibility,
                         MemberEligibility.WAITING)
        decision = await FleetPolicy(self.svc).evaluate("bravo")
        self.assertFalse(decision.allowed)             # policy gates it

    async def test_reconnect_restores_eligible(self):
        await self.intake.observe(Observation(
            source_account="bravo",
            observation_type="account_disconnected", message_id="dc@1"))
        await self.intake.observe(Observation(
            source_account="bravo",
            observation_type="account_connected", message_id="dc@2"))
        self.assertEqual(self.svc.member("bravo").eligibility,
                         MemberEligibility.ELIGIBLE)

    async def test_operator_pause_outranks_connectivity(self):
        """A PAUSED member stays paused through disconnect/reconnect —
        connectivity never undoes an operator decision."""
        await self.svc.pause_member("bravo")
        await self.intake.observe(Observation(
            source_account="bravo",
            observation_type="account_disconnected", message_id="dc@1"))
        await self.intake.observe(Observation(
            source_account="bravo",
            observation_type="account_connected", message_id="dc@2"))
        self.assertEqual(self.svc.member("bravo").eligibility,
                         MemberEligibility.PAUSED)

    async def test_reconnect_of_eligible_member_is_a_noop(self):
        handled = await self.intake.observe(Observation(
            source_account="bravo",
            observation_type="account_connected", message_id="dc@1"))
        self.assertTrue(handled)                       # accepted...
        self.assertEqual(self.svc.member("bravo").eligibility,
                         MemberEligibility.ELIGIBLE)   # ...state unchanged


class CapabilityTests(unittest.TestCase):
    def test_default_off_ping_untouched(self):
        class Simple(PluginBaseStub):
            pass
        self.assertIsNone(handler_of(Simple()))
        self.assertEqual(definitions_of(Simple()), [])

    def test_fleet_handler_mounts_with_validated_definitions(self):
        handler = SampleHandler()
        plugin = PluginBaseStub()
        plugin.fleet_handler = handler
        self.assertIs(handler_of(plugin), handler)
        self.assertEqual([d.id for d in definitions_of(plugin)], ["roll"])

    def test_non_handler_attribute_never_mounts(self):
        plugin = PluginBaseStub()
        plugin.fleet_handler = "not a handler"
        self.assertIsNone(handler_of(plugin))

    def test_bad_declaration_fails_loudly(self):
        class BadHandler(FleetTaskHandler):
            def task_definitions(self):
                return [{"id": "not-a-definition"}]
        plugin = PluginBaseStub()
        plugin.fleet_handler = BadHandler()
        with self.assertRaises(ValueError):
            definitions_of(plugin)

    def test_execute_required_by_default(self):
        async def run():
            await FleetTaskHandler().execute(None, None, None)
        with self.assertRaises(NotImplementedError):
            import asyncio
            asyncio.run(run())


class PluginBaseStub:
    """Duck-typed plugin stand-in (handler_of is attribute-based)."""
    fleet_handler = None


class SampleHandler(FleetTaskHandler):
    def task_definitions(self):
        return (ROLL,)

    async def execute(self, ctx, assignment, prepared):
        return None


if __name__ == "__main__":
    unittest.main()
