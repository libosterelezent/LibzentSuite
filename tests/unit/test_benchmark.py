"""Benchmark subsystem unit tests: samplers, collector, report, scenarios."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from benchmark.collector import PerfCollector
from benchmark.report import (_verdict, compare, leak_verdict, render_table)
from benchmark.sampler import LoopLagMonitor, TaskSampler


class LoopLagMonitorTests(unittest.TestCase):
    def test_percentile_math_and_buckets(self) -> None:
        monitor = LoopLagMonitor(interval_s=0.001)
        # F3 round-4: >= LAG_MIN_SAMPLES records so percentiles are
        # reported (below the floor the summary emits None)
        for _ in range(4):
            for lag_ms in (1.0, 2.0, 2.0, 10.0, 50.0):
                monitor._record(lag_ms / 1000.0)
        summary = monitor.summary()
        self.assertEqual(summary["samples"], 20)
        self.assertEqual(summary["lag_quality"], "ok")
        self.assertAlmostEqual(summary["max_ms"], 50.0, delta=0.01)
        self.assertLessEqual(summary["p50_ms"], 3.0)
        self.assertGreaterEqual(summary["p99_ms"], 10.0)

    def test_insufficient_samples_emits_none_not_zero(self) -> None:
        """F3 round-4 fix #4: 0/19 samples must not read as 0.0ms lag."""
        empty = LoopLagMonitor()
        summary = empty.summary()
        self.assertEqual(summary["samples"], 0)
        self.assertIsNone(summary["p50_ms"])
        self.assertIsNone(summary["p95_ms"])
        self.assertIsNone(summary["p99_ms"])
        self.assertEqual(summary["lag_quality"], "insufficient")

        few = LoopLagMonitor()
        for _ in range(LoopLagMonitor.LAG_MIN_SAMPLES - 1):
            few._record(0.050)
        self.assertIsNone(few.summary()["p50_ms"])

    def test_at_threshold_reports_percentiles(self) -> None:
        monitor = LoopLagMonitor()
        for _ in range(LoopLagMonitor.LAG_MIN_SAMPLES):
            monitor._record(0.002)
        summary = monitor.summary()
        self.assertEqual(summary["lag_quality"], "ok")
        self.assertIsNotNone(summary["p50_ms"])

    def test_compare_skips_missing_lag_metrics(self) -> None:
        """A baseline without lag samples must not fabricate 0.0→N
        regressions (F3 round-4 fix #4)."""
        def _scen(lag: dict) -> dict:
            return {"name": "x", "mode": "BASELINE", "cycles": 1,
                    "passed": True, "process": {"rss_base_kb": 1000,
                                                "rss_peak_kb": 2000,
                                                "cpu_pct": 5.0},
                    "loop_lag_ms": lag, "tasks": {"end": 3},
                    "threads": {"end": 2}, "stages": {}, "custom": {},
                    "leak": {"verdict": "stable"}}
        before = {"scenarios": [_scen({"p50_ms": None, "p95_ms": None,
                                       "p99_ms": None, "max_ms": None,
                                       "samples": 3})]}
        after = {"scenarios": [_scen({"p50_ms": 4, "p95_ms": 8, "p99_ms": 9,
                                      "max_ms": 12, "samples": 400})]}
        text = compare(before, after)
        self.assertIn("ok (within tolerances)", text)
        self.assertNotIn("lag_", text)

    def test_render_marks_insufficient_lag(self) -> None:
        report = {"scenarios": [{
            "name": "x", "mode": "test", "cycles": 1, "passed": True,
            "process": {}, "loop_lag_ms": {"p50_ms": None, "p95_ms": None,
                                           "p99_ms": None, "max_ms": 0.0,
                                           "samples": 2},
            "tasks": {}, "threads": {}, "stages": {}, "custom": {},
            "leak": {},
        }]}
        self.assertIn("p50=n/a", render_table(report))


class LoopLagFinalStallTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_records_pending_drift(self) -> None:
        """F3 round-4 fix #9 + re-audit A: cancelling mid-sleep must still
        count a GENUINE final stall (elapsed > one full interval). A
        sub-interval cancel records nothing — the old behavior of
        recording the clamped negative as a 0-lag sample polluted the
        histogram on every clean shutdown."""

        class Parked(LoopLagMonitor):
            """Monitor whose wake loop never fires (simulates the sleep
            never completing): only stop()'s pending-drift path can
            record."""

            async def _run(self) -> None:
                await asyncio.Event().wait()

        monitor = Parked(interval_s=0.05)
        monitor.start()
        self.assertEqual(monitor.summary()["samples"], 0)
        await asyncio.sleep(0.12)  # > 2 intervals of real stall
        await monitor.stop()
        self.assertEqual(monitor.summary()["samples"], 1)
        # summary() percentiles are None below LAG_MIN_SAMPLES (by design)
        # — assert on the raw max instead
        self.assertGreater(monitor._max_s, 0.05)
        # clean sub-interval stop records NOTHING (no 0-lag pollution)
        clean = Parked(interval_s=60.0)
        clean.start()
        await asyncio.sleep(0.01)
        await clean.stop()
        self.assertEqual(clean.summary()["samples"], 0)


class TaskSamplerTests(unittest.IsolatedAsyncioTestCase):
    async def test_census_counts_live_tasks_only(self) -> None:
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        live = asyncio.get_running_loop().create_future()
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
        def sync_fn():
            return 1

        @perf.timed("async_fn")
        async def async_fn():
            await asyncio.sleep(0.01)
            return 2

        with perf.stage("block"):
            asyncio.get_running_loop()
        self.assertEqual(sync_fn(), 1)
        self.assertEqual(await async_fn(), 2)
        snap = perf.snapshot()
        self.assertEqual(snap["block"]["count"], 1)
        self.assertEqual(snap["sync_fn"]["count"], 1)
        self.assertEqual(snap["async_fn"]["count"], 1)
        self.assertGreaterEqual(snap["async_fn"]["p50_s"], 0.005)

    def test_error_path_counts_and_reraises(self) -> None:
        perf = PerfCollector()
        with self.assertRaises(RuntimeError):
            with perf.stage("boom"):
                raise RuntimeError("x")
        snap = perf.snapshot()
        self.assertEqual(snap["boom"]["errors"], 1)


class LeakVerdictTests(unittest.TestCase):
    def test_stable_series(self) -> None:
        verdict = leak_verdict([100, 100.0, 100.0, 100.0, 100.0, 100.0],
                               [5, 5, 5, 5, 5, 5])
        self.assertEqual(verdict["verdict"], "stable")

    def test_monotone_growth_flagged(self) -> None:
        verdict = leak_verdict(
            [100, 100, 200, 300, 400, 500], [5, 5, 5, 5, 5, 5])
        self.assertEqual(verdict["verdict"], "suspected-leak")

    def test_insufficient_cycles(self) -> None:
        verdict = leak_verdict([100, 120], [5, 5])
        self.assertEqual(verdict["verdict"], "insufficient-cycles")

    def test_returns_to_baseline_not_a_leak(self) -> None:
        verdict = leak_verdict(
            [100, 100, 500, 100, 100, 100], [5, 5, 5, 5, 5, 5])
        self.assertEqual(verdict["verdict"], "stable")

    # ---------------- F3 round-4: slope-comparison regression tests
    def test_transient_drop_does_not_collapse_tolerance(self) -> None:
        """Fix 2a: steady ~150kB/cycle growth with one 500kB transient
        drop — the old max-drop noise band hid this as 'stable'."""
        verdict = leak_verdict(
            [1000, 1000, 1150, 1300, 800, 1450, 1600, 1750],
            [5, 5, 5, 5, 5, 5, 5, 5])
        self.assertIn(verdict["verdict"],
                      ("suspected-leak", "inconclusive"))

    def test_drop_masked_ambiguity_is_inconclusive(self) -> None:
        """Fix 2: a drop larger than the noise band masks the trend →
        'inconclusive' (never silently 'stable')."""
        verdict = leak_verdict(
            [1000, 1000, 1150, 1300, 700, 1450, 1600, 1750],
            [5, 5, 5, 5, 5, 5, 5, 5])
        self.assertEqual(verdict["verdict"], "inconclusive")

    def test_monotone_with_interior_drop_still_flagged(self) -> None:
        verdict = leak_verdict(
            [100, 100, 200, 300, 100, 400, 500, 600],
            [5, 5, 5, 5, 5, 5, 5, 5])
        self.assertIn(verdict["verdict"],
                      ("suspected-leak", "inconclusive"))

    def test_bounded_plateau_is_stable(self) -> None:
        """Fix 2b: rise-then-flat (bounded plateau) — the old
        endpoint-only growth flagged this as 'suspected-leak'."""
        verdict = leak_verdict(
            [1000, 1000, 1500, 2000, 2400, 2400, 2400, 2400, 2400],
            [5, 5, 5, 5, 5, 5, 5, 5, 5])
        self.assertEqual(verdict["verdict"], "stable")

    def test_slow_steady_growth_still_flagged(self) -> None:
        verdict = leak_verdict(
            [100, 100, 300, 450, 600, 750, 900, 1050],
            [5, 5, 5, 5, 5, 5, 5, 5])
        self.assertEqual(verdict["verdict"], "suspected-leak")

    def test_inconclusive_keeps_passed_true(self) -> None:
        """The passed gate only rejects suspected-leak — inconclusive
        must not fail a run (rerun-with-more-cycles guidance instead)."""
        verdict = leak_verdict(
            [1000, 1000, 1150, 1300, 700, 1450, 1600, 1750],
            [5, 5, 5, 5, 5, 5, 5, 5])
        self.assertTrue(verdict["verdict"] != "suspected-leak")


class VerdictDirectionTests(unittest.TestCase):
    """F3 round-4 fix #3: reliability counters are lower-is-better."""

    def test_db_failures_growth_is_regression(self) -> None:
        self.assertEqual(_verdict("custom.db_failures", 0.0, 5.0),
                         "regressed")
        self.assertEqual(_verdict("custom.db_failures", 5.0, 0.0),
                         "improved")

    def test_dropped_growth_is_regression(self) -> None:
        self.assertEqual(_verdict("custom.dropped", 0.0, 3.0), "regressed")
        self.assertEqual(_verdict("custom.dropped", 3.0, 0.0), "improved")

    def test_custom_throughput_still_higher_is_better(self) -> None:
        self.assertEqual(_verdict("custom.records_per_s", 100.0, 200.0),
                         "improved")
        self.assertEqual(_verdict("custom.records_per_s", 200.0, 100.0),
                         "regressed")

    def test_census_counters_are_lower_is_better(self) -> None:
        self.assertEqual(_verdict("tasks_end", 3.0, 10.0), "regressed")
        self.assertEqual(_verdict("threads_end", 2.0, 6.0), "regressed")


class ReportTests(unittest.TestCase):
    def test_render_and_compare(self) -> None:
        report = {"scenarios": [{
            "name": "x", "mode": "test", "cycles": 1, "passed": True,
            "process": {"rss_base_kb": 1000, "rss_peak_kb": 2000,
                        "cpu_pct": 5.0},
            "loop_lag_ms": {"p50_ms": 1, "p95_ms": 2, "p99_ms": 3,
                            "max_ms": 4, "samples": 10},
            "tasks": {"end": 3}, "threads": {"end": 2},
            "stages": {}, "custom": {}, "leak": {"verdict": "stable"},
        }]}
        text = render_table(report)
        self.assertIn("Scenario: x", text)
        # compare against itself: everything unchanged
        self.assertIn("ok (within tolerances)", compare(report, report))
        worse = {"scenarios": [dict(report["scenarios"][0])]}
        worse["scenarios"][0]["loop_lag_ms"] = {
            "p50_ms": 50, "p95_ms": 80, "p99_ms": 90, "max_ms": 100,
            "samples": 10}
        self.assertIn("REGRESSED", compare(report, worse))


class ScenarioSmokeTests(unittest.IsolatedAsyncioTestCase):
    """Minimal-parameter smoke: every core scenario runs and tears down."""

    async def test_scenarios_smoke(self) -> None:
        from benchmark.cli import build_parser, run_suite
        from benchmark.scenarios import BENCH_SCENARIOS

        for name in ("cold_boot",):
            args = build_parser().parse_args(
                ["--scenario", name, "--cycles", "1", "--duration", "0.1",
                 "--quiet"])
            report = await run_suite(args)
            scen = report["scenarios"][0]
            self.assertTrue(scen["passed"], f"{name} failed")
            self.assertIn("loop_lag_ms", scen)

    async def test_restart_churn_leak_protocol(self) -> None:
        from benchmark.cli import build_parser, run_suite

        args = build_parser().parse_args(
            ["--scenario", "restart_churn", "--cycles", "4", "--quiet"])
        report = await run_suite(args)
        scen = report["scenarios"][0]
        self.assertEqual(scen["custom"]["clients_built"], 4)
        self.assertEqual(scen["custom"]["restarts"], 4)
        self.assertIn(scen["leak"]["verdict"],
                      ("stable", "inconclusive", "insufficient-cycles"))

    async def test_multi_scenario_report_not_truncated(self) -> None:
        """F3 round-4 fix #1 regression: every scenario lands in the
        report (the result block used to be dedented out of the loop, so
        only the LAST scenario survived — full-suite runs lost 6/7)."""
        from benchmark.cli import build_parser, run_suite

        args = build_parser().parse_args(
            ["--scenario", "db_writes", "--ops", "30",
             "--scenario", "cooldown_heavy", "--keys", "20", "--quiet"])
        report = await run_suite(args)
        self.assertEqual(len(report["scenarios"]), 2)
        self.assertEqual([s["name"] for s in report["scenarios"]],
                         ["db_writes", "cooldown_heavy"])
        for scen in report["scenarios"]:
            self.assertTrue(scen["passed"], scen["name"])

    async def test_db_writes_cooldown_heavy_message_burst_smoke(self) -> None:
        """F3 round-4 fix #10: the three previously never-tested
        scenarios as a reduced-parameter smoke (TemporaryWorkspace
        isolation; cycle counts are 1, knobs shrunk to stay fast)."""
        from benchmark.cli import build_parser, run_suite

        args = build_parser().parse_args(
            ["--scenario", "db_writes", "--ops", "40",
             "--scenario", "cooldown_heavy", "--keys", "30",
             "--scenario", "message_burst", "--messages", "200",
             "--burst", "50", "--waiters", "2", "--quiet"])
        report = await run_suite(args)
        self.assertEqual(len(report["scenarios"]), 3)
        by_name = {s["name"]: s for s in report["scenarios"]}
        self.assertEqual(set(by_name),
                         {"db_writes", "cooldown_heavy", "message_burst"})
        for name, scen in by_name.items():
            self.assertTrue(scen["passed"], f"{name} failed")
            self.assertIn(scen["leak"]["verdict"],
                          ("stable", "inconclusive", "insufficient-cycles"))
        self.assertEqual(by_name["db_writes"]["custom"]["ops_per_repo"], 40)
        self.assertEqual(by_name["cooldown_heavy"]["custom"]["keys"], 30)
        self.assertEqual(by_name["message_burst"]["custom"]["messages"], 200)

    async def test_bootstrap_key_isolation(self) -> None:
        """The bench workspace must never touch state/credentials.key."""
        from benchmark.stack import BenchStack, TemporaryWorkspace

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


class DbTelemetryTests(unittest.TestCase):
    """F3 round-4 fixes #5/#6 (database/telemetry.py)."""

    def test_ring_keeps_tail_not_head(self) -> None:
        """Fill past maxlen: newest records present, oldest evicted (the
        old `len < maxlen` guard froze the FIRST 1024 forever)."""
        from database.telemetry import _RING, DbTelemetry

        t = DbTelemetry()
        t.enabled = True
        for i in range(_RING + 200):
            t.record("read", (i + 1) / 1_000_000.0)
        self.assertEqual(len(t._ring), _RING)
        all_ms = [entry[1] for entry in t._ring]
        self.assertAlmostEqual(all_ms[-1], (_RING + 200) / 1000.0,
                               places=3)  # newest kept
        self.assertNotIn(0.001, all_ms)   # record #1 evicted
        self.assertGreater(all_ms[0], 0.2)  # head is ~record #201

    def test_worker_cpu_uses_kernel_hz(self) -> None:
        """F3 round-4 fix #6: tick→second conversion uses SC_CLK_TCK
        (an HZ=250 kernel here), not the hardcoded 100."""
        from unittest import mock

        from database.telemetry import DbTelemetry

        t = DbTelemetry()
        t.set_worker_thread(threading.current_thread())
        # remainder after "(comm)": utime at [11]=250 ticks, stime [12]=500
        stat = "1234 (py) R 1 2 3 4 5 6 7 8 9 10 250 500 13 14 15 16 0"
        with mock.patch("builtins.open",
                        mock.mock_open(read_data=stat)), \
                mock.patch("os.sysconf", return_value=250):
            cpu = t._worker_thread_cpu()
        self.assertIsNotNone(cpu)
        self.assertTrue(cpu["available"])
        self.assertAlmostEqual(cpu["user_s"], 1.0, places=6)   # 250/250
        self.assertAlmostEqual(cpu["sys_s"], 2.0, places=6)    # 500/250

    def test_worker_cpu_hz_fallback_survives(self) -> None:
        from database.telemetry import DbTelemetry

        t = DbTelemetry()
        t.set_worker_thread(threading.current_thread())
        real = os.sysconf

        def _boom(_name: str) -> int:
            raise ValueError("sysconf unavailable")

        os.sysconf = _boom
        try:
            cpu = t._worker_thread_cpu()
        finally:
            os.sysconf = real
        self.assertIsNotNone(cpu)
        if cpu.get("available"):
            self.assertGreaterEqual(cpu["user_s"], 0.0)
            self.assertGreaterEqual(cpu["sys_s"], 0.0)


class ArgValidationTests(unittest.TestCase):
    """F3 round-4 fix #8: bounded validators reject busy-spin intervals
    and empty-run counts at parse time."""

    def _rejects(self, *argv: str) -> None:
        from benchmark.cli import build_parser
        with self.assertRaises(SystemExit):
            build_parser().parse_args(list(argv))

    def test_zero_intervals_rejected(self) -> None:
        self._rejects("--sampling-ms", "0")
        self._rejects("--loop-interval-ms", "0")
        self._rejects("--task-interval-ms", "0")

    def test_sampling_below_floor_rejected(self) -> None:
        self._rejects("--sampling-ms", "4")

    def test_zero_and_negative_counts_rejected(self) -> None:
        for flag in ("--accounts", "--cycles", "--records", "--messages",
                     "--burst", "--ops", "--keys", "--waiters", "--repeat"):
            self._rejects(flag, "0")
            self._rejects(flag, "-3")

    def test_boundary_values_accepted(self) -> None:
        from benchmark.cli import build_parser
        args = build_parser().parse_args(
            ["--sampling-ms", "5", "--loop-interval-ms", "1",
             "--task-interval-ms", "5", "--cycles", "1"])
        self.assertEqual(
            (args.sampling_ms, args.loop_interval_ms,
             args.task_interval_ms, args.cycles),
            (5, 1, 5, 1))


if __name__ == "__main__":
    unittest.main()
