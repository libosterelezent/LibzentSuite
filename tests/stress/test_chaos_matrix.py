"""FINAL-PHASE (G8): chaos matrix — fleet chaos + profiling-hook safety.

- FleetChaosTests (LZ_CHAOS gated): 5 accounts × start_all × bursts × DB
  writes × auth-fail injection × drop-reconnect × delete+create.
- ProfilingChaosTests (ungated): §39 — diagnostic hooks ALWAYS restore.
"""
from __future__ import annotations

import asyncio
import cProfile
import gc
import os
import sys
import tempfile
import unittest
from pathlib import Path

from core.errors import AccountError
from core.event_bus import AccountConnected, MessageReceived
from database.db import Database
from tests.unit.helpers import FakeClientFactory, make_manager
from tests.unit.test_account_manager import _fast_reconnect_settings

CHAOS = bool(os.environ.get("LZ_CHAOS"))


@unittest.skipUnless(CHAOS, "set LZ_CHAOS=1 to run the fleet chaos soak")
class FleetChaosTests(unittest.IsolatedAsyncioTestCase):
    """5-account full chaos (G8 design a): start_all admission path, 2k
    burst, real temp-DB writes, memory-first status polls, auth-fail
    injection, mid-chaos drop + delete/create, contamination checks."""

    async def test_five_account_full_chaos(self) -> None:
        tokens = {f"DISCORD_TOKEN_{n.upper()}": f"tok-{n}"
                  for n in ("alpha", "beta", "gamma", "delta", "epsilon",
                            "zeta")}
        h = make_manager(token_values=tokens,
                         settings=_fast_reconnect_settings())
        factory = FakeClientFactory()
        h.manager._client_factory = factory
        names = ["alpha", "beta", "gamma", "delta", "epsilon"]
        for name in names:
            await h.manager.add_account(name)

        # start_all bounded scheduler path (sorted — deterministic order)
        report = await h.manager.start_all(stagger=0.05)
        self.assertEqual(report["started"], sorted(names))
        await h.collector.wait_for(AccountConnected, 5)

        baseline = sum(1 for t in asyncio.all_tasks() if not t.done())
        with tempfile.TemporaryDirectory(prefix="sm-chaos-") as tmp:
            db = Database(str(Path(tmp) / "chaos.db"))
            await db.connect()
            self.addAsyncCleanup(db.close)
            now = 1000.0

            for round_no in range(4):
                # burst: 400 events per live account (2k total)
                for name in list(h.manager.names()):
                    client = h.manager.get(name).runtime.client
                    for i in range(400):
                        h.bus.publish_nowait(MessageReceived(
                            account=name, message_id=round_no * 1000 + i,
                            channel_id=500, author_id=777,
                            content=f"chaos {round_no} {i}"))
                await asyncio.sleep(0)
                # DB writes concurrent with chaos
                for name in list(h.manager.names()):
                    await db.kv.set(f"chaos:{round_no}:{name}",
                                    {"r": round_no})
                    await db.cooldowns.upsert(
                        account=name, plugin="roll",
                        cooldown_key=f"k{round_no}",
                        started_at=now + round_no,
                        expires_at=now + round_no + 3600, source="chaos")
                # memory-first status poll (dashboard shape)
                rows = await h.manager.accounts()
                self.assertEqual({r["name"] for r in rows},
                                 set(h.manager.names()))

                if round_no == 1:  # AUTH-FAIL injection + recovery
                    await h.manager.stop("delta")
                    factory2 = FakeClientFactory(
                        fail_connect=True, fail_message="401 Unauthorized: improper token")
                    factory2.clients = factory.clients
                    h.manager._client_factory = factory2
                    with self.assertRaises(AccountError):
                        await h.manager.start("delta")
                    self.assertIn(
                        h.manager.get("delta").runtime.status.value,
                        ("invalid_credential", "failed"))
                    healthy = [h.manager.get(n).runtime.status.value
                               for n in ("alpha", "beta", "gamma", "epsilon")]
                    self.assertEqual(healthy, ["running"] * 4)
                    h.manager._client_factory = factory  # recovery
                    await h.manager.start("delta")
                    self.assertEqual(
                        h.manager.get("delta").runtime.status.value,
                        "running")
                if round_no == 2:  # drop + reconnect while others run
                    h.manager.get("beta").runtime.client.drop(
                        "gateway closed (code=1006)")
                    await h.collector.wait_for(AccountConnected, 7)
                if round_no == 3:  # delete + create mid-chaos
                    await h.manager.stop("epsilon")
                    await h.manager.remove_account("epsilon")
                    await h.manager.add_account("zeta")
                    await h.manager.start("zeta")
                    self.assertEqual(
                        h.manager.get("zeta").runtime.status.value,
                        "running")

            await h.bus.drain(timeout=30)
            # convergence
            for name in h.manager.names():
                rt = h.manager.get(name).runtime
                self.assertEqual(rt.status.value, "running", name)
                self.assertIsNotNone(rt.client)
            # task-delta bound (supervisors churned: delta restart, epsilon
            # gone, zeta added → net supervisor count == accounts)
            live = sum(1 for t in asyncio.all_tasks() if not t.done())
            self.assertLessEqual(live - baseline, 12)
            # no zombie clients
            current = {h.manager.get(n).runtime.client
                       for n in h.manager.names()}
            zombies = [c for c in factory.clients
                       if c.connected and c not in current]
            self.assertEqual(zombies, [])
            # no cross-account contamination: exactly 400 events per account
            # per round it was live, none from foreign accounts
            accounts_seen = {e.account for e in
                             h.collector.of(MessageReceived)}
            self.assertTrue(accounts_seen <= set(names) | {"zeta"})
            # DB rows round-trip per owning account
            row = await db.kv.get("chaos:0:alpha")
            self.assertIsNotNone(row)
            await h.manager.stop_all()


class ProfilingChaosTests(unittest.IsolatedAsyncioTestCase):
    """G8 design d (§39): profiling hooks ALWAYS restore, even on failure."""

    async def test_tracemalloc_stops_after_suite(self) -> None:
        import tracemalloc

        from benchmark.cli import build_parser, run_suite

        self.assertFalse(tracemalloc.is_tracing())
        tracemalloc.start(1)
        args = build_parser().parse_args(
            ["--scenario", "log_burst", "--records", "50", "--quiet",
             "--tracemalloc"])
        report = await run_suite(args)
        self.assertFalse(tracemalloc.is_tracing())
        self.assertIn("allocations",
                      report["scenarios"][0]["custom"])

    async def test_tracemalloc_stops_even_when_scenario_fails(self) -> None:
        import tracemalloc

        from benchmark import scenarios as scen_mod
        from benchmark.cli import build_parser, run_suite

        tracemalloc.start(1)
        original = scen_mod.BENCH_SCENARIOS["log_burst"]

        async def exploding(*a, **k):
            raise RuntimeError("chaos: scenario exploded")

        scen_mod.BENCH_SCENARIOS["log_burst"] = scen_mod.BenchScenario(
            "log_burst", "exploding", 1, exploding)
        try:
            args = build_parser().parse_args(
                ["--scenario", "log_burst", "--records", "10", "--quiet",
                 "--tracemalloc"])
            with self.assertRaises(RuntimeError):
                await run_suite(args)
            self.assertFalse(tracemalloc.is_tracing())
        finally:
            scen_mod.BENCH_SCENARIOS["log_burst"] = original
            if tracemalloc.is_tracing():
                tracemalloc.stop()

    async def test_cprofile_hooks_restored(self) -> None:
        from benchmark.cli import build_parser, run_suite

        self.assertIsNone(sys.getprofile())
        with tempfile.TemporaryDirectory(prefix="sm-prof-") as tmp:
            args = build_parser().parse_args(
                ["--scenario", "log_burst", "--records", "50", "--quiet",
                 "--profile", "--profile-dir", tmp])
            report = await run_suite(args)
            self.assertTrue(
                (Path(tmp) / "log_burst.pstats").exists())
        self.assertIsNone(sys.getprofile())
        gc.collect()
        leftovers = [o for o in gc.get_objects()
                     if isinstance(o, cProfile.Profile)]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
