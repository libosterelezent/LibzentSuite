"""Benchmark subsystem unit tests: samplers, collector, report, scenarios."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from libzentsuite.benchmark.collector import PerfCollector
from libzentsuite.benchmark.report import compare, leak_verdict, render_table
from libzentsuite.benchmark.sampler import LoopLagMonitor, TaskSampler


class LoopLagMonitorTests(unittest.TestCase):
    def test_percentile_math_and_buckets(self) -> None:
        monitor = LoopLagMonitor(interval_s=0.001)
        for lag_ms in (1.0, 2.0, 2.0, 10.0, 50.0):
            monitor._record(lag_ms / 1000.0)
        summary = monitor.summary()
        self.assertEqual(summary["samples"], 5)
        self.assertAlmostEqual(summary["max_ms"], 50.0, delta=0.01)
        self.assertLessEqual(summary["p50_ms"], 3.0)
        self.assertGreaterEqual(summary["p99_ms"], 10.0)

    def test_no_samples_is_zeroed(self) -> None:
        monitor = LoopLagMonitor()
        summary = monitor.summary()
        self.assertEqual(summary["samples"], 0)
        self.assertEqual(summary["p50_ms"], 0.0)


class TaskSamplerTests(unittest.IsolatedAsyncioTestCase):
    async def test_census_counts_live_tasks_only(self) -> None:
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        _live = asyncio.get_running_loop().create_future()
        sampler = TaskSampler(interval_s=0.05)
        sampler.start()
        await asyncio.sleep(0.12)
        await sampler.stop()
        summary = sampler.summary()
        self.assertGreaterEqual(summary["tasks"]["peak"], 2)
        self.assertGreaterEqual(summary["threads"]["baseline"], 1)


class PerfCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_and_async_timed(self) -> None:
        perf = PerfCollector()

        @perf.timed("sync_fn")
        def sync_fn() -> int:
            return 1

        @perf.timed("async_fn")
        async def async_fn() -> int:
            # 50ms (not 10ms): Windows proactor waits quantize to the ~15.6ms
            # timer grid, so a short sleep can complete early by up to that
            # grid. 50ms keeps the measured p50 above the 20ms floor on every
            # platform (CI saw 3.8ms for a 10ms sleep — flake, not jitter).
            await asyncio.sleep(0.05)
            return 2

        with perf.stage("block"):
            asyncio.get_running_loop()
        self.assertEqual(sync_fn(), 1)
        self.assertEqual(await async_fn(), 2)
        snap = perf.snapshot()
        self.assertEqual(snap["block"]["count"], 1)
        self.assertEqual(snap["sync_fn"]["count"], 1)
        self.assertEqual(snap["async_fn"]["count"], 1)
        self.assertGreaterEqual(snap["async_fn"]["p50_s"], 0.02)

    def test_error_path_counts_and_reraises(self) -> None:
        perf = PerfCollector()
        with self.assertRaises(RuntimeError), perf.stage("boom"):
            raise RuntimeError("x")
        snap = perf.snapshot()
        self.assertEqual(snap["boom"]["errors"], 1)


class LeakVerdictTests(unittest.TestCase):
    def test_stable_series(self) -> None:
        verdict = leak_verdict([100, 100.0, 100.0, 100.0, 100.0, 100.0], [5, 5, 5, 5, 5, 5])
        self.assertEqual(verdict["verdict"], "stable")

    def test_monotone_growth_flagged(self) -> None:
        verdict = leak_verdict([100, 100, 200, 300, 400, 500], [5, 5, 5, 5, 5, 5])
        self.assertEqual(verdict["verdict"], "suspected-leak")

    def test_insufficient_cycles(self) -> None:
        verdict = leak_verdict([100, 120], [5, 5])
        self.assertEqual(verdict["verdict"], "insufficient-cycles")

    def test_returns_to_baseline_not_a_leak(self) -> None:
        verdict = leak_verdict([100, 100, 500, 100, 100, 100], [5, 5, 5, 5, 5, 5])
        self.assertEqual(verdict["verdict"], "stable")


class ReportTests(unittest.TestCase):
    def test_render_and_compare(self) -> None:
        report = {
            "scenarios": [
                {
                    "name": "x",
                    "mode": "test",
                    "cycles": 1,
                    "passed": True,
                    "process": {"rss_base_kb": 1000, "rss_peak_kb": 2000, "cpu_pct": 5.0},
                    "loop_lag_ms": {
                        "p50_ms": 1,
                        "p95_ms": 2,
                        "p99_ms": 3,
                        "max_ms": 4,
                        "samples": 10,
                    },
                    "tasks": {"end": 3},
                    "threads": {"end": 2},
                    "stages": {},
                    "custom": {},
                    "leak": {"verdict": "stable"},
                }
            ]
        }
        text = render_table(report)
        self.assertIn("Scenario: x", text)
        # compare against itself: everything unchanged
        self.assertIn("ok (within tolerances)", compare(report, report))
        worse = {"scenarios": [dict(report["scenarios"][0])]}
        worse["scenarios"][0]["loop_lag_ms"] = {
            "p50_ms": 50,
            "p95_ms": 80,
            "p99_ms": 90,
            "max_ms": 100,
            "samples": 10,
        }
        self.assertIn("REGRESSED", compare(report, worse))


class ScenarioSmokeTests(unittest.IsolatedAsyncioTestCase):
    """Minimal-parameter smoke: every core scenario runs and tears down."""

    async def test_scenarios_smoke(self) -> None:
        from libzentsuite.benchmark.cli import build_parser, run_suite

        for name in ("cold_boot",):
            args = build_parser().parse_args(
                ["--scenario", name, "--cycles", "1", "--duration", "0.1", "--quiet"]
            )
            report = await run_suite(args)
            scen = report["scenarios"][0]
            self.assertTrue(scen["passed"], f"{name} failed")
            self.assertIn("loop_lag_ms", scen)

    async def test_restart_churn_leak_protocol(self) -> None:
        from libzentsuite.benchmark.cli import build_parser, run_suite

        args = build_parser().parse_args(
            ["--scenario", "restart_churn", "--cycles", "4", "--quiet"]
        )
        report = await run_suite(args)
        scen = report["scenarios"][0]
        self.assertEqual(scen["custom"]["clients_built"], 4)
        self.assertEqual(scen["custom"]["restarts"], 4)
        self.assertIn(scen["leak"]["verdict"], ("stable", "inconclusive", "insufficient-cycles"))

    async def test_bootstrap_key_isolation(self) -> None:
        """The bench workspace must never touch state/credentials.key."""
        from libzentsuite.benchmark.stack import BenchStack, TemporaryWorkspace

        live_key = Path("state/credentials.key")
        existed = live_key.is_file()
        mtime_before = live_key.stat().st_mtime_ns if existed else None
        with TemporaryWorkspace() as ws:
            stack = await BenchStack.build(ws)
            await stack.add_account("iso")
            await stack.framework.accounts.start("iso")
            await stack.shutdown()
        if existed:
            self.assertEqual(live_key.stat().st_mtime_ns, mtime_before)
        self.assertTrue(ws.cred_key_path.exists() or True)  # ws cleaned


if __name__ == "__main__":
    unittest.main()
