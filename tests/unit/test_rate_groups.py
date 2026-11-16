"""Two-scope rate limiting tests (spec: per-account/group + per-task).

Per-TASK scope: consecutive API calls (tasks) of ONE account are spaced —
``("account", action)`` pacing, the pre-existing behavior.

Per-ACCOUNT/GROUP scope: accounts sharing a group serialize against ONE
shared clock — A -> [limit] -> B -> [limit] -> A — while accounts in
DIFFERENT groups (or ungrouped) stay independent. Both scopes compose (max).
"""
from __future__ import annotations

import asyncio
import unittest

from core.event_bus import EventBus
from tests.unit.test_rate_engine import SleepAdvanceManager, StubLogger

ACTION = "poke"  # no learned default — floor semantics testable


def make_group_manager():
    from core.rate_limit_manager.engine import ManualClock

    manager = SleepAdvanceManager(
        EventBus(), StubLogger(), None, clock=ManualClock(0.0), jitter_seed=7
    )
    return manager


class TestGroupScope(unittest.IsolatedAsyncioTestCase):
    async def test_group_accounts_serialize_on_one_clock(self) -> None:
        """A -> [limit] -> B -> [limit] -> A: members of one group share a
        pacing clock even though each alternates."""
        manager = make_group_manager()
        manager.set_policy_values(ACTION, 2.0)
        manager.set_account_group("alpha", "grinders")
        manager.set_account_group("beta", "grinders")

        times = []

        async def send(account):
            await manager.acquire(ACTION, account=account)
            times.append((account, manager._clock.now()))

        await send("alpha")                       # t=0
        await send("beta")                        # waits group clock -> t=2
        await send("alpha")                       # waits again -> t=4
        self.assertEqual([t for _, t in times], [0.0, 2.0, 4.0])

    async def test_different_groups_independent(self) -> None:
        manager = make_group_manager()
        manager.set_policy_values(ACTION, 2.0)
        manager.set_account_group("alpha", "one")
        manager.set_account_group("beta", "two")
        await manager.acquire(ACTION, account="alpha")   # t=0
        await manager.acquire(ACTION, account="beta")    # different clock -> t=0
        self.assertEqual(manager._clock.now(), 0.0)

    async def test_grouped_and_ungrouped_compose(self) -> None:
        """A grouped account respects BOTH its own (per-task) clock and the
        group clock — the stricter wait wins."""
        manager = make_group_manager()
        manager.set_policy_values(ACTION, 2.0)
        manager.set_account_group("alpha", "g")
        await manager.acquire(ACTION, account="alpha")   # t=0 (both clocks)
        await manager.acquire(ACTION, account="beta")    # beta joins group now
        manager.set_account_group("beta", "g")
        # beta fires at t=2 (group clock); alpha's own clock ALSO reads t=0,
        # so alpha's next send must wait from the group stamp at t=2 -> t=4
        await manager.acquire(ACTION, account="beta")
        self.assertEqual(manager._clock.now(), 2.0)
        await manager.acquire(ACTION, account="alpha")
        self.assertEqual(manager._clock.now(), 4.0)

    async def test_group_removal_returns_to_per_task_only(self) -> None:
        manager = make_group_manager()
        manager.set_policy_values(ACTION, 2.0)
        manager.set_account_group("alpha", "g")
        manager.set_account_group("beta", "g")
        await manager.acquire(ACTION, account="alpha")   # t=0, stamps both
        manager.set_account_group("beta", None)          # beta leaves
        await manager.acquire(ACTION, account="beta")    # own clock empty -> t=0
        self.assertEqual(manager._clock.now(), 0.0)
        self.assertEqual(manager.account_groups(), {"alpha": "g"})

    async def test_validation(self) -> None:
        manager = make_group_manager()
        from core.errors import RatePolicyError

        with self.assertRaises(RatePolicyError):
            manager.set_account_group("", "g")

    async def test_group_clock_in_stats(self) -> None:
        manager = make_group_manager()
        manager.set_account_group("alpha", "grinders")
        stats = manager.stats()
        self.assertEqual(stats["groups"], {"alpha": "grinders"})
        await manager.acquire(ACTION, account="alpha")
        stats = manager.stats()
        self.assertIn(f"group:grinders:{ACTION}", stats["last_fire_ages"])
        self.assertIn(f"alpha:{ACTION}", stats["last_fire_ages"])

    async def test_interleaved_concurrent_group_members(self) -> None:
        """True interleaving: both members pending at once still serialize
        (the grant pass stamps the shared clock before the other grants)."""
        manager = make_group_manager()
        manager.set_policy_values(ACTION, 1.5)
        manager.set_account_group("alpha", "g")
        manager.set_account_group("beta", "g")
        grant_times = []

        async def worker(account):
            for _ in range(2):
                await manager.acquire(ACTION, account=account)
                grant_times.append((account, manager._clock.now()))

        await asyncio.gather(worker("alpha"), worker("beta"))
        ordered = sorted(t for _, t in grant_times)
        gaps = [b - a for a, b in zip(ordered, ordered[1:])]
        self.assertTrue(all(g >= 1.45 for g in gaps), gaps)
        # both group members actually got to send
        senders = {account for account, _ in grant_times}
        self.assertEqual(senders, {"alpha", "beta"})

    async def test_cancelled_member_does_not_corrupt_group_clock(self) -> None:
        """A member cancelled mid-wait leaves the shared stamp intact AND
        consumes no virtual time (gated sleep never ran): the next member
        still waits out the full interval from the LAST grant, never less."""
        from core.rate_limit_manager.engine import ManualClock
        from tests.unit.test_rate_engine import GatedSleepManager

        manager = GatedSleepManager(
            EventBus(), StubLogger(), None, clock=ManualClock(0.0),
            jitter_seed=7,
        )
        manager.set_policy_values(ACTION, 2.0)
        manager.set_account_group("alpha", "g")
        manager.set_account_group("beta", "g")
        await manager.acquire(ACTION, account="alpha")  # stamps both at t=0
        doomed = asyncio.ensure_future(
            manager.acquire(ACTION, account="beta")
        )
        for _ in range(50):
            if manager.gates:
                break
            await asyncio.sleep(0)  # beta parked behind a gated 2s sleep
        doomed.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await doomed
        self.assertEqual(manager._clock.now(), 0.0)  # clock untouched
        grant_task = asyncio.ensure_future(
            manager.acquire(ACTION, account="alpha")
        )
        for _ in range(50):
            if len(manager.gates) >= 2:
                break
            await asyncio.sleep(0)
        manager.gates[-1][1].set()  # release alpha's pacing sleep -> t=2
        grant = await grant_task
        self.assertAlmostEqual(grant.wait_s, 2.0, places=6)

    async def test_concurrent_members_with_cancellation_keep_group_gaps(self) -> None:
        """Chaos variant: three members racing with one cancellation —
        every shared-clock grant stamp stays >= interval apart (measured on
        exact stamps, not task-resume times)."""
        from core.rate_limit_manager.engine import ManualClock
        from tests.unit.test_rate_engine import GrantRecorderManager

        interval = 1.5
        manager = GrantRecorderManager(
            EventBus(), StubLogger(), None,
            clock=ManualClock(0.0), jitter_seed=3,
        )
        manager.set_policy_values(ACTION, interval)
        for account in ("alpha", "beta", "gamma"):
            manager.set_account_group(account, "g")

        async def worker(account):
            await manager.acquire(ACTION, account=account)

        tasks = [
            asyncio.ensure_future(worker(a))
            for a in ("alpha", "beta", "gamma", "alpha", "beta")
        ]
        await asyncio.sleep(0)
        tasks[1].cancel()  # one member dies mid-wait
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            self.assertNotIsInstance(result, Exception)
        group_stamps = sorted(
            ts for key, ts in manager.grant_log if key == ("group:g", ACTION)
        )
        self.assertGreaterEqual(len(group_stamps), 4)
        gaps = [b - a for a, b in zip(group_stamps, group_stamps[1:])]
        self.assertTrue(all(g >= interval - 1e-9 for g in gaps), gaps)


class TestGroupWireGapsWithJitter(unittest.IsolatedAsyncioTestCase):
    """Round-3 wire-gap fix (probe P1): jitter is drawn BEFORE admission
    and the shared group clock is stamped at grant + jitter (the expected
    wire instant) — a much smaller second draw can no longer compress the
    members' WIRE gap to interval - jitter_max (0.07s here) or invert it."""

    async def test_group_wire_gap_holds_under_jitter_differential(self) -> None:
        from core.rate_limit_manager.engine import ManualClock

        interval = 1.5
        # seed 15: draws ~1.948 then ~0.517 in (0.5, 2.0) — under the old
        # post-grant-only jitter the second wire left just 0.07s after the
        # first (configured spacing 1.5s)
        manager = SleepAdvanceManager(
            EventBus(), StubLogger(), None, clock=ManualClock(0.0),
            jitter_seed=15,
        )
        manager.set_policy_values(ACTION, interval, 0.5, 2.0)
        manager.set_account_group("alpha", "grinders")
        manager.set_account_group("beta", "grinders")

        wires = []

        async def send(account):
            await manager.acquire(ACTION, account=account)
            wires.append((account, manager._clock.now()))

        await send("alpha")
        await send("beta")
        await send("alpha")
        gaps = [wires[i + 1][1] - wires[i][1] for i in range(len(wires) - 1)]
        self.assertTrue(all(g >= interval - 1e-9 for g in gaps), gaps)
        # the alternating members really serialized on one shared clock
        self.assertEqual([a for a, _ in wires], ["alpha", "beta", "alpha"])


class TestGroupStampCleanup(unittest.IsolatedAsyncioTestCase):
    """Round-3: ('group:<x>', action) stamps must not outlive the group —
    when the LAST member leaves (set_account_group(None) or prune), the
    shared stamps go with it; a re-created same-named group starts fresh."""

    async def test_last_member_leaving_drops_group_stamps(self) -> None:
        manager = make_group_manager()
        manager.set_policy_values(ACTION, 2.0)
        manager.set_account_group("alpha", "g")
        manager.set_account_group("beta", "g")
        await manager.acquire(ACTION, account="alpha")  # stamps group:g
        manager.set_account_group("alpha", None)  # one member remains
        self.assertIn(("group:g", ACTION), manager._last_fire)
        manager.set_account_group("beta", None)  # LAST member out
        self.assertNotIn(("group:g", ACTION), manager._last_fire)
        # a re-created same-named group is not delayed by the old stamps
        manager.set_account_group("gamma", "g")
        grant = await manager.acquire(ACTION, account="gamma")
        self.assertEqual(grant.wait_s, 0.0)

    async def test_group_stamps_survive_while_members_remain(self) -> None:
        manager = make_group_manager()
        manager.set_policy_values(ACTION, 2.0)
        manager.set_account_group("alpha", "g")
        manager.set_account_group("beta", "g")
        await manager.acquire(ACTION, account="alpha")
        manager.set_account_group("alpha", "other")  # alpha moves away
        self.assertIn(("group:g", ACTION), manager._last_fire)  # beta still g
        await manager.acquire(ACTION, account="beta")  # still spaced by g
        self.assertEqual(manager._clock.now(), 2.0)

    async def test_prune_account_drops_orphaned_group_stamps(self) -> None:
        manager = make_group_manager()
        manager.set_policy_values(ACTION, 2.0)
        manager.set_account_group("alpha", "g")
        await manager.acquire(ACTION, account="alpha")
        self.assertIn(("group:g", ACTION), manager._last_fire)
        await manager.prune_account("alpha")  # last (only) member removed
        self.assertNotIn(("group:g", ACTION), manager._last_fire)
        self.assertEqual(manager.account_groups(), {})


if __name__ == "__main__":
    unittest.main()
