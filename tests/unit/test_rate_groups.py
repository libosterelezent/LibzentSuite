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
from core.rate_limit_manager import RateLimitManager

from tests.unit.test_rate_engine import SleepAdvanceManager, StubLogger, make_manager

ACTION = "message"


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
        self.assertIn("group:grinders:message", stats["last_fire_ages"])
        self.assertIn("alpha:message", stats["last_fire_ages"])

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
        from tests.unit.test_rate_engine import GrantRecorderManager
        from core.rate_limit_manager.engine import ManualClock

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


if __name__ == "__main__":
    unittest.main()
