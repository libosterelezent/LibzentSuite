"""Benchmark CLI (house style of scripts/bench_db.py). Routed from
``main.py --bench`` BEFORE any live boot, or runnable as
``python -m benchmark``."""
from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys
import time
from types import SimpleNamespace as NS
from typing import Any, Dict, List

from benchmark import SCHEMA
from benchmark.collector import PerfCollector
from benchmark.report import compare, load_json, render_table, to_json
from benchmark.sampler import SamplerSession

from benchmark.scenarios import BENCH_SCENARIOS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmark",
        description="SlaughterMudade framework benchmark (isolated, "
                    "fakes/simulation only — never real Discord, never "
                    "state/slaughter.db)")
    parser.add_argument("--scenario", action="append", default=None,
                        help="scenario name (repeatable; default: all)")
    parser.add_argument("--list", action="store_true",
                        help="list scenarios and exit")
    parser.add_argument("--accounts", type=int, default=5)
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument("--cycles", type=int, default=None,
                        help="override the scenario's default cycle count")
    parser.add_argument("--records", type=int, default=10_000)
    parser.add_argument("--messages", type=int, default=5_000)
    parser.add_argument("--burst", type=int, default=200)
    parser.add_argument("--ops", type=int, default=2_000)
    parser.add_argument("--keys", type=int, default=2_000)
    parser.add_argument("--waiters", type=int, default=4)
    parser.add_argument("--severity", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--sampling-ms", type=int, default=100)
    parser.add_argument("--loop-interval-ms", type=int, default=5,
                        help="1 = stall-hunt mode")
    parser.add_argument("--task-interval-ms", type=int, default=1000)
    parser.add_argument("--json", default=None, help="write JSON report")
    parser.add_argument("--save-baseline", action="store_true",
                        help="also store the report as the compare baseline")
    parser.add_argument("--compare", default=None,
                        help="compare against a previous JSON report")
    parser.add_argument("--quiet", action="store_true")
    return parser


def _list_scenarios() -> str:
    lines = ["AVAILABLE SCENARIOS", "=" * 60]
    for s in BENCH_SCENARIOS.values():
        lines.append(f"{s.name:<22} cycles={s.default_cycles:<3} {s.description}")
    return "\n".join(lines)


async def run_suite(args: NS) -> Dict[str, Any]:
    names = args.scenario or list(BENCH_SCENARIOS)
    unknown = [n for n in names if n not in BENCH_SCENARIOS]
    if unknown:
        raise SystemExit(
            f"unknown scenario(s) {unknown}; known: {list(BENCH_SCENARIOS)}")
    report: Dict[str, Any] = {
        "schema": SCHEMA,
        "meta": {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime()),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "argv": sys.argv[1:],
        },
        "config": {
            "sampling_ms": args.sampling_ms,
            "loop_interval_ms": args.loop_interval_ms,
            "task_interval_ms": args.task_interval_ms,
        },
        "scenarios": [],
    }
    started = time.perf_counter()
    for name in names:
        scenario = BENCH_SCENARIOS[name]
        cycles = args.cycles if args.cycles is not None else scenario.default_cycles
        if not args.quiet:
            print(f"== running {name} (cycles={cycles}) ...", flush=True)
        session = SamplerSession(
            sampling_ms=args.sampling_ms,
            loop_interval_ms=args.loop_interval_ms,
            task_interval_ms=args.task_interval_ms)
        perf = PerfCollector()
        # reset the per-run leak trackers (module-level series)
        scenario.run._rss_series = []
        scenario.run._task_series = []
        result = await scenario.run(
            session, perf, accounts=args.accounts, duration=args.duration,
            cycles=cycles, records=args.records, messages=args.messages,
            burst=args.burst, ops=args.ops, keys=args.keys,
            waiters=args.waiters, severity=args.severity)
        result["name"] = name
        result["cycles"] = cycles
        result.setdefault("mode", "production-boot")
        report["scenarios"].append(result)
    report["meta"]["duration_s"] = round(time.perf_counter() - started, 2)
    return report


def run_bench_cli(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        print(_list_scenarios())
        return 0
    report = asyncio.run(run_suite(args))
    print(render_table(report))
    json_path = args.json
    if json_path is None and args.save_baseline:
        os.makedirs("state/benchmarks", exist_ok=True)
        json_path = "state/benchmarks/baseline.json"
    if json_path:
        to_json(report, json_path)
        print(f"JSON report: {json_path}")
    if args.save_baseline and not json_path:
        to_json(report, "state/benchmarks/baseline.json")
    if args.compare:
        before = load_json(args.compare)
        print(compare(before, report))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_bench_cli())
