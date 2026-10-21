"""LibzentSuite framework benchmark subsystem (opt-in, isolated).

Public surface: ``BENCH_SCENARIOS``, ``run_suite`` (async) and
``run_bench_cli``. Routed from ``main.py --bench`` or ``python -m
benchmark``; never imported by production paths, so normal runtime
carries zero benchmark overhead. See benchmark/README.md.
"""
from benchmark.report import SCHEMA

from benchmark.cli import run_bench_cli
from benchmark.scenarios import BENCH_SCENARIOS, BenchScenario

__all__ = ["BENCH_SCENARIOS", "BenchScenario", "run_bench_cli", "SCHEMA"]
