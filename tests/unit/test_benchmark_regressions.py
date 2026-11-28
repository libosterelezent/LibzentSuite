"""Regression tests for audit fixes: run_suite multi-scenario reporting,
lower-is-better verdicts for end-state/counter metrics, compare() missing-
scenario detection."""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from libzentsuite.benchmark.report import _verdict, compare
from libzentsuite.benchmark.scenarios import BenchScenario


async def _stub_run(session: Any, perf: Any, **kw: Any) -> dict[str, Any]:
    return {
        "passed": True,
        "process": {},
        "loop_lag_ms": {},
        "tasks": {},
        "threads": {},
        "stages": {},
        "custom": {},
        "leak": {"verdict": "stable"},
    }


def _stub_scenarios() -> dict[str, BenchScenario]:
    return {
        "stub_one": BenchScenario("stub_one", "regression stub", 1, _stub_run),
        "stub_two": BenchScenario("stub_two", "regression stub", 1, _stub_run),
    }


class RunSuiteMultiScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_scenarios_reported(self) -> None:
        from libzentsuite.benchmark.cli import build_parser, run_suite

        args = build_parser().parse_args(["--quiet"])
        with mock.patch("benchmark.cli.BENCH_SCENARIOS", _stub_scenarios()):
            report = await run_suite(args)
        self.assertEqual([s["name"] for s in report["scenarios"]], ["stub_one", "stub_two"])

    async def test_repeat_aggregation_lands_per_scenario(self) -> None:
        from libzentsuite.benchmark.cli import build_parser, run_suite

        args = build_parser().parse_args(["--repeat", "2", "--quiet"])
        with mock.patch("benchmark.cli.BENCH_SCENARIOS", _stub_scenarios()):
            report = await run_suite(args)
        self.assertEqual(len(report["scenarios"]), 2)
        for scen in report["scenarios"]:
            self.assertEqual(scen["repeat"]["n"], 2)
            self.assertTrue(scen["passed"])


class VerdictLowerIsBetterTests(unittest.TestCase):
    def test_end_state_counters_are_lower_is_better(self) -> None:
        self.assertEqual(_verdict("tasks_end", 5.0, 50.0), "regressed")
        self.assertEqual(_verdict("threads_end", 2.0, 20.0), "regressed")
        self.assertEqual(_verdict("tasks_end", 50.0, 5.0), "improved")
        self.assertEqual(_verdict("threads_end", 5.0, 6.0), "unchanged")

    def test_counter_style_custom_metrics_are_lower_is_better(self) -> None:
        self.assertEqual(_verdict("custom.db_failures", 0.0, 10.0), "regressed")
        self.assertEqual(_verdict("custom.dropped", 0.0, 5.0), "regressed")
        self.assertEqual(_verdict("custom.errors", 0.0, 4.0), "regressed")
        self.assertEqual(_verdict("custom.restarts", 1.0, 10.0), "improved")


def _scen(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "mode": "test",
        "cycles": 1,
        "passed": True,
        "process": {"rss_base_kb": 1000, "rss_peak_kb": 2000, "cpu_pct": 5.0},
        "loop_lag_ms": {"p50_ms": 1, "p95_ms": 2, "p99_ms": 3, "max_ms": 4, "samples": 10},
        "tasks": {"end": 3},
        "threads": {"end": 2},
        "stages": {},
        "custom": {},
        "leak": {"verdict": "stable"},
    }


class CompareMissingScenarioTests(unittest.TestCase):
    def test_missing_baseline_scenario_flags_regression(self) -> None:
        before = {"scenarios": [_scen("a"), _scen("b")]}
        after = {"scenarios": [_scen("a")]}
        text = compare(before, after)
        self.assertIn("MISSING", text)
        self.assertIn("b", text)
        self.assertIn("REGRESSED", text)

    def test_empty_after_report_fails_loudly(self) -> None:
        before = {"scenarios": [_scen("a")]}
        with self.assertRaises(ValueError):
            compare(before, {"scenarios": []})

    def test_same_scenarios_still_ok(self) -> None:
        report = {"scenarios": [_scen("a"), _scen("b")]}
        self.assertIn("ok (within tolerances)", compare(report, report))


if __name__ == "__main__":
    unittest.main()
