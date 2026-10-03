# Framework Benchmark Subsystem

Opt-in, isolated CPU/RAM/event-loop benchmarking. Never imports into
production paths (zero overhead when unused), never touches
`state/slaughter.db` or `state/credentials.key` (temp workspaces + the
bootstrap's `credential_key_path` kwarg), never talks to Discord (fakes
and simulation only).

## Run

```bash
python main.py --bench                          # full default suite
python main.py --bench cold_boot                # one scenario
python main.py --bench --list                   # scenario registry
python main.py --bench restart_churn --cycles 10
python main.py --bench log_burst --records 20000 --severity DEBUG
python main.py --bench --json out.json --save-baseline
python main.py --bench --compare state/benchmarks/baseline.json
python -m benchmark --scenario message_burst --loop-interval-ms 1  # stall-hunt
```

## Scenarios

| name | measures | default cycles |
|---|---|---|
| `cold_boot` | full real `bootstrap.build()` + shutdown wall/RSS/lag | 3 |
| `accounts_startstop` | N-account start/stop cost (fake clients, real stack) | 3 |
| `restart_churn` | start→stop leak series per restart | 8 |
| `log_burst` | LogService record flood + persistence drain health | 1 |
| `db_writes` | kv/cooldown/log sustained writes + loop stall | 1 |
| `cooldown_heavy` | cooldown set/read/restore hot paths | 1 |
| `message_burst` | bus fan-out × plugin dispatch × waiter flood | 1 |

## Metrics

- Process: `rss_base/peak/end_kb`, `cpu_pct`, `cpu_user_s`, `cpu_sys_s`,
  `fd_peak` (resource + /proc, stdlib only — no psutil dependency).
- Event loop: sleep-drift lag histogram (p50/p95/p99/max; 0.25ms buckets
  to 100ms, 1ms to 2s; default 5ms interval, `--loop-interval-ms 1` for
  stall hunting).
- Tasks/threads: 1 Hz census (`asyncio.all_tasks()` is itself costly at
  high frequency — deliberately decoupled from the 10 Hz process sampler).
- Stages: per-label wall times (count/total/p50/p95/max) via `PerfCollector`.
- Leak: per-cycle post-quiesce (gc + settle) RSS/task series with a
  TREND verdict — `stable` / `suspected-leak` / `inconclusive` — never an
  absolute-threshold guess. Cycle 1 is warmup and excluded from trend math.

## Compare / baselines

`--save-baseline` stores the run at `state/benchmarks/baseline.json`
(gitignored). `--compare` prints before/after/Δ with per-metric verdicts
using documented noise tolerances (`benchmark/report.py::TOLERANCES`).
A single `REGRESSED` verdict means: **rerun with more cycles and compare
medians** before believing it — wall times on a shared machine are noisy.

## Profiling modes

- **normal** — the `benchmark` package is never imported; zero overhead.
- **benchmark** — samplers + stage timers (this mode; <1% CPU).
- **`--profile`** — cProfile per scenario: `.pstats` dump under
  `state/benchmarks/profiles/` (or `--profile-dir`) + top-N cumulative/
  tottime embedded in the JSON. Main-thread profiler (the aiosqlite
  worker thread's cost shows in cpu_sys_s but not function attribution).
- **`--tracemalloc`** — allocation sites per scenario boundary
  (`--trace-depth`, `--trace-top`); per-stage diffs via the existing
  `perf.stage()` seams; `clear_traces` + `reset_peak` between scenarios
  keep attribution honest.

Profiled runs carry `profiled: true` and are **excluded from compare
verdicts** (profiler overhead makes their wall/CPU numbers
non-comparable — ~2-5x inflation for cProfile, ~2x for tracemalloc);
`--save-baseline` is refused under `--profile`, and the two modes are
mutually exclusive. Output surfaces are value-free by construction
(pstats records filenames/line numbers/counts; tracemalloc records
file:lineno + byte counts — neither captures argument or object values).

## Design notes

- `TimedProxy` instrumentation wraps collaborators at constructor seams —
  NEVER client objects (the account manager's staleness guards compare
  `rt.client is client`; a proxy would defeat them).
- The simulator's pacing floor (1.5s) dominates wall time in traffic
  scenarios — compare per-stage CPU and events/s, not raw wall.
- REPL/dashboard integration is deliberately absent: an in-process bench
  would measure (and perturb) the live stack. `--bench` runs pre-boot in
  an isolated process, mirroring `--sim`.
