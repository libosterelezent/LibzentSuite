# BENCHMARKING — the framework benchmark subsystem

> Source of truth: `benchmark/` package + `benchmark/README.md`.
> Verified at commit `487dc53`.

Opt-in, isolated CPU/RAM/event-loop benchmarking. Never imported by
production paths (zero overhead when unused); never touches
`state/slaughter.db` or `state/credentials.key` (temp workspaces + the
bootstrap's `credential_key_path` kwarg); never talks to Discord.

## Run

```bash
python main.py --bench                          # full default suite
python main.py --bench cold_boot                # one scenario
python main.py --bench --list                   # scenario registry
python main.py --bench restart_churn --cycles 10
python main.py --bench message_burst --repeat 3 # medians (compare-grade)
python main.py --bench --json out.json --save-baseline
python main.py --bench --compare state/benchmarks/baseline.json
python main.py --bench --capture-spikes         # DEEP_DIAGNOSTIC
python -m benchmark --scenario log_burst --loop-interval-ms 1  # stall-hunt
```

## Scenarios
`cold_boot` (real bootstrap+shutdown) · `accounts_startstop` (N-account
through the real stack) · `restart_churn` (start/stop leak series) ·
`log_burst` · `db_writes` · `cooldown_heavy` · `message_burst`
(bus fan-out + plugin dispatch flood).

## Metrics
Process (RSS/CPU/FD via stdlib `resource` + `/proc`), event-loop lag
(sleep-drift histogram, p50/p95/p99/max; 5 ms default, 1 ms stall-hunt),
task/thread census (1 Hz), stage timings (`PerfCollector`), DB worker
telemetry (G1), leak verdicts from repeated-cycle trends (stable /
suspected-leak — no absolute thresholds), run-mode labels (BASELINE /
PROFILED / MEMORY_PROFILED / DEEP_DIAGNOSTIC; profiled runs are
compare-excluded), spike capture (self-relative thresholds, debounced).

## Profiling modes
`--profile` = MAIN_THREAD_FUNCTION_PROFILE (cProfile; aiosqlite worker
invisible except cpu_sys). `--tracemalloc` = per-stage allocation diffs
(value-free). Worker telemetry = WORKER_THREAD_TIMING (boundary totals;
commit/transaction split real; per-statement split honestly documented
as unavailable). All hooks restore in `finally` — even on scenario
failure (chaos-tested).
