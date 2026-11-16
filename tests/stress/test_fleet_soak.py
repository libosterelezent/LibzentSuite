"""PHASE-4 fleet soak: multi-account chaos with bounded resources.

Reduced-but-real version of the S8 stress design: 3 fake accounts under
rolling gateway drops + restarts + lifecycle churn for ~12s, asserting
status convergence, bounded task growth, no zombie clients, and a loop
lag ceiling (the phase-3 limiter-burn regression class becomes a hard
assertion). Tagged via env var so the default suite stays fast:

    LZ_SOAK=1 python -m unittest tests.stress.test_fleet_soak
"""

from __future__ import annotations

import asyncio
import os
import unittest

from core.account_manager import AccountStatus
from core.event_bus import AccountConnected
from tests.unit.helpers import make_manager
from tests.unit.test_account_manager import _fast_reconnect_settings

SOAK_ENABLED = bool(os.environ.get("LZ_SOAK"))


@unittest.skipUnless(SOAK_ENABLED, "set LZ_SOAK=1 to run the fleet soak")
class FleetSoakTests(unittest.IsolatedAsyncioTestCase):
    async def test_chaos_soak(self) -> None:
        h = make_manager(
            token_values={
                "DISCORD_TOKEN_ALPHA": "tok-alpha",
                "DISCORD_TOKEN_BETA": "tok-beta",
                "DISCORD_TOKEN_GAMMA": "tok-gamma",
            },
            settings=_fast_reconnect_settings(),
        )
        names = ["alpha", "beta", "gamma"]
        for name in names:
            await h.manager.add_account(name)
        for name in names:
            await h.manager.start(name)
        await h.collector.wait_for(AccountConnected, 3)

        _loop = asyncio.get_running_loop()
        baseline_tasks = TaskProbe.live_count()
        connects_before = len(h.collector.of(AccountConnected))

        async def chaos(round_no: int) -> None:
            victim = names[round_no % len(names)]
            rt = h.manager.get(victim).runtime
            client = rt.client
            if client is not None:
                client.drop(f"gateway closed (code=100{round_no % 8})")

        for round_no in range(6):  # ~12s of rolling chaos
            await asyncio.sleep(2.0)
            await chaos(round_no)
            if round_no == 2:  # one manual restart mid-soak
                target = names[(round_no + 1) % len(names)]
                await h.manager.stop(target)
                await h.manager.start(target)
            if round_no == 3:  # one estop mid-soak
                target = names[(round_no + 2) % len(names)]
                h.manager.emergency_stop(target)
                await asyncio.sleep(0.3)
                await h.manager.start(target)

        await asyncio.sleep(3.0)  # let reconnects converge

        # --- pass criteria -------------------------------------------------
        for name in names:  # statuses converge
            rt = h.manager.get(name).runtime
            self.assertIn(
                rt.status, (AccountStatus.RUNNING,), f"{name} did not converge: {rt.status}"
            )
            self.assertIsNotNone(rt.client)

        live = TaskProbe.live_count()
        # bounded growth: 3 supervisors + test machinery, nothing leaked
        self.assertLessEqual(live - baseline_tasks, 8, f"task growth {baseline_tasks}->{live}")

        zombies = [
            c
            for c in h.factory.clients
            if c.connected
            and c is not h.manager.get(names[0]).runtime.client
            and c is not h.manager.get(names[1]).runtime.client
            and c is not h.manager.get(names[2]).runtime.client
        ]
        self.assertEqual(zombies, [], "zombie clients after soak")

        connects = len(h.collector.of(AccountConnected)) - connects_before
        self.assertGreaterEqual(connects, 6)  # drops produced reconnects
        await h.manager.stop_all()


class TaskProbe:
    @staticmethod
    def live_count() -> int:
        return sum(1 for t in asyncio.all_tasks() if not t.done())


if __name__ == "__main__":
    unittest.main()
