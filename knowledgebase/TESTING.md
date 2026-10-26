# TESTING — the real suite layout

The project's testing stance is **offline-only**: every test runs without
network, tokens or a real Discord account. The Discord boundary is replaced
either by in-memory fakes (unit) or by the simulator adapter (integration /
simulation), which is the "Testing adapter" role of the adapter boundary.

---

## 1. Layout + counts

```
tests/
  unit/          517 test methods — managers, engine, CLI, dashboard, plugins
  integration/   12 test methods  — full production stack over the simulator
  simulation/    21 test methods  — scenario registry + scenario runs
  fixtures/      fixture plugins (echo, spy) + a bad plugin (plugins_bad)
  unit/helpers.py  shared duck-typed fakes (repos, clients, harness)
```

Total: **550 `def test_` methods** (counted via grep across the tree; matches
the ~550 figure — the root README's "450-test suite" line is stale).

| File | Test methods | One-line purpose (from module docstrings) |
|---|---|---|
| `tests/unit/test_account_manager.py` | 45 | Account manager: registry, lifecycle, supervisor, send hook, details page shape, inbound audit |
| `tests/unit/test_account_services.py` | 25 | Account services: task-ack sends, waits, collect idle-timeout semantics, cache-first DMs, passthrough helpers |
| `tests/unit/test_api_cache.py` | 5 | Persistent API cache: per-account entity caching (anti-flag trim), 3-layer DM resolution, invalidation on `TargetGone`, DB-cached `fetch_user` |
| `tests/unit/test_audit.py` | 20 | Audit: ring, tail filters, DB sink, history, format |
| `tests/unit/test_cli_commands.py` | 50 | CLI command engine + REPL against a fake ControlAPI (no real managers) |
| `tests/unit/test_dashboard_http.py` | 45 | Dashboard over real HTTP: REST auth, routing, validation, static index (fake ControlAPI) |
| `tests/unit/test_dashboard_ws.py` | 22 | WebSocket: RFC 6455 subset unit tests + live server behavior (handshake, `refresh`, log/audit/task pushes) |
| `tests/unit/test_database.py` | 18 | Database layer: migration idempotence, every repository round-trip, cascade deletes, pruning, in-memory mode |
| `tests/unit/test_logging_service.py` | 20 | Logging feedback-loop regression: "N source events == N canonical LogService events", stderr-exactly-once, severity mapping, bridge detach, shutdown ordering |
| `tests/unit/test_plugin_manager.py` | 13 | Plugin manager: discovery, attach/activate, toggles, dispatch isolation |
| `tests/unit/test_profiles.py` | 22 | Profile manager: CRUD, assignment, allowed_plugins |
| `tests/unit/test_rate_engine.py` | 39 | Pacing engine: pacing basics, per-account vs global keys, jitter bounds, priority/FIFO ordering, server cooldowns, events, stats, floor raise-only, wake-on-policy-change, waiter cancellation, input validation |
| `tests/unit/test_rate_groups.py` | 7 | Two-scope rate limiting: per-task spacing + per-account/group serialization, both compose (max) |
| `tests/unit/test_rate_lane.py` | 15 | GlobalSendLane: strict serialization + prefixrate spacing, priority, pause-at-0 + resume, disable-mid-wait drain, wake-on-config-change, kv persistence, validation |
| `tests/unit/test_rate_policies.py` | 42 | Policy model: validation, scope parsing (level/owner rules), set/clear incl. safety floor, persistence round-trip |
| `tests/unit/test_rate_warnings.py` | 16 | Warning taxonomy (aggressive / no_jitter / narrow_jitter / floor), ordering, manager integration |
| `tests/unit/test_remote_control.py` | 11 | RemoteControl: mention-prefix management from Discord (fakes only) |
| `tests/unit/test_rich_model.py` | 22 | Rich message/event model: lossless conversion, frozen DTO isolation, runtime-only `raw` escape hatch, typed events without fetches, reaction/button actions, future-plugin shapes |
| `tests/unit/test_roll.py` | 4 | Roll plugin: owner trigger → one pipelined send |
| `tests/unit/test_sim_client.py` | 16 | SimClient: the simulated half of the adapter boundary (offline, synthetic) |
| `tests/unit/test_sim_world.py` | 15 | SimWorld: a complete in-memory Discord world (synthetic ids) |
| `tests/unit/test_soulcopy.py` | 11 | Soulcopy: textutils pipeline + the full flow over fake services |
| `tests/unit/test_task_scheduler.py` | 34 | Task scheduler: lanes, priority, retry, cancel, history |
| `tests/integration/test_full_stack.py` | 12 | Full-stack: the PRODUCTION `bootstrap.build()` composition with only the Discord boundary swapped for the simulator |
| `tests/simulation/test_scenarios.py` | 21 | Scenario registry tests + full-stack runs (runner + all managers + real plugins) |

---

## 2. Determinism techniques

| Technique | Where | How |
|---|---|---|
| `ManualClock` | rate engine / lane tests | Injected `Clock` implementation; `advance()` moves time without sleeping (production uses monotonic `SystemClock`) |
| `_sleep` seam | rate engine, lane, scheduler tests | Every timed wait goes through `await self._sleep(s)`; test doubles advance the manual clock instead of blocking (gated variants for wake tests); the scheduler's `_sleep` seam is patched for backoff |
| Seeded RNG | jitter draws | `RateLimitManager(jitter_seed=…)` seeds `random.Random` for reproducible jitter; scenario pacing checks use fixed tolerances (`PACING_TOLERANCE = 0.05`) |
| Duck-typed fakes | `tests/unit/helpers.py` | `FakeAccountsRepo`/`FakeProfilesRepo` (dict-backed, fault-togglable), `FakePluginManager`, `FakeScheduler`, `FakeSettings`, `FakeAudit`, `FakeClient`/`FakeClientFactory` (in-memory Discord client with canned READY snapshots + `drop()`/`receive()`), `ManagerHarness`, `make_task` |
| Fixture plugins via the loader | `tests/fixtures/plugins/{echo.py,spy/__init__.py}`, `tests/fixtures/plugins_bad/badname.py` | Tests build a real `PluginLoader(FIXTURES)` over the fixture dir (spy records every event/hook call per instance → routing/isolation assertions; `badname` must be rejected with `PluginError`) |
| Bounded waits | `EventCollector.wait_for` | Polls for N bus events with a hard timeout and a descriptive `AssertionError` |
| Synthetic ids | simulator world | All scenario ids are module constants (`SYNTHETIC_*`); real ids/tokens only ever live in `.env` |
| In-memory DB | scenarios, DB tests | `Database(":memory:")` per run |

---

## 3. Simulation scenarios (`adapters/simulator/scenarios.py`)

Each `Scenario` bundles `policies` (applied pre-run, clamped to the 1.5 s
floor), `world_setup`, `script` and pure post-mortem `checks` ([] == pass).
Runnable via `python3 main.py --sim <name>` or `simulation run <name>`.

| Name | Description (verbatim) |
|---|---|
| `soulcopy_basic` | Owner triggers slcopy; Mudae answers with 2 short pages and the plugin delivers a single `$soulcopy` chunk back to the channel |
| `soulcopy_multichunk` | A long roster forces the chunker to emit several chunks, each within the 2000-char Discord limit (prefix included) |
| `pacing_respected` | With a 1.6 s message interval every outbound gap observed in the audit trail is at least the interval (minus a 0.05 s tolerance) |
| `server_429_respected` | A simulated 429 (Retry-After 0.8 s) blocks the export send; the framework reports it, waits, retries and still completes the flow |
| `channel_gone_aborts` | The trigger channel is deleted mid-flow (after the export, before the chunks); the flow aborts cleanly and no chunk is ever sent |
| `control_mention` | The owner manages the fleet from Discord by MENTIONING the account as the command prefix (`@account status`, `estop`); replies arrive through the paced pipeline and non-owners are ignored |

---

## 4. Running the suite

```bash
bash scripts/run_tests.sh                      # = python3 -m unittest discover -s tests -t . -v
python3 -m unittest discover -s tests -t . -v  # the same, verbose
python3 -m unittest tests.unit.test_rate_lane  # one module
python3 main.py --sim-list                     # list scenarios
python3 main.py --sim pacing_respected         # run one scenario (exit 1 on FAIL)
```

No network, no tokens, no dependencies beyond the standard library.

## 5. What simulation covers vs the live suite

The simulator (`adapters/simulator/`) covers the **complete production
stack**: `bootstrap.build()` composition, managers, scheduler, limiter,
database, audit, logging and the real plugins — only the Discord client is
the in-memory `SimClient` over a `SimWorld` (scripted Mudae responder,
synthetic ids, optional latency/429s/channel deletions).

A `tests_live/` package (live-mode suite) and `scripts/preflight_live.py`
existed historically but were **removed in the `b5c2769` rewrite**; the
current stance is offline-only. Live Discord behaviour (real
`discord.py-self` login, genuine 429 headers, gateway resumes) is therefore
**not** covered by automated tests — exercise it manually with
`--start-all` on disposable accounts.


---

## Post-merge suite structure (1191 tests)

> Verified at commit `487dc53`.

* `tests/stress/` — `LZ_SOAK`-gated fleet soak (3-account chaos),
  `LZ_CHAOS`-gated 5-account full chaos (auth-fail injection,
  drop-reconnect, delete+create mid-chaos), profiling-hook restoration.
* `tests/unit/test_tombstone.py` — weakref-proven post-delete audit:
  Account/runtime/client/supervisor COLLECTED after removal, task delta
  back to baseline, loggerDict bounded by live accounts.
* `tests/unit/test_lifecycle.py` — governor units + manager integration.
* `tests/unit/test_hardening_phase4.py` — generation race matrix,
  concurrent starts, estop-overtake, stagger pinning.
* `tests/unit/test_final_phase.py` — waiter registry, effective cache,
  memory-first status, scheduler pressure, gate depth, security.
* `tests/unit/test_residual_risk.py` — telemetry, pressure, dispatch
  cache equivalence, bench reliability, artifact security.
* `tests/unit/test_event_bus.py` — coalesced dispatch invariants
  (completeness, per-account FIFO, overflow, slice yielding, reentrancy,
  deferral invariant).
* `tests/unit/test_audit_duplicate_fix.py` — attach_audit idempotence.

### Addendum (2026-10-26): uv-locked workflow is the canonical test/CI path

Since the ruff/mypy/uv adoption (2026-10-25, AGENTS.md §8), the canonical
commands are:

    uv sync --locked                      # exact CI environment from uv.lock
    uv run --frozen ruff check main.py core adapters interfaces database benchmark scripts
    uv run --frozen ruff format --check main.py core adapters interfaces database benchmark scripts
    uv run --frozen mypy                  # advisory ratchet (baseline tracked)
    uv run --frozen python -m unittest discover -s tests -t .

CI (tests.yml) runs exactly these via `uv sync --locked` + `uv run --frozen`;
`requirements.txt` is the generated pip-compatible export of `uv.lock`
(regenerate with `uv export` — never hand-edit). Everything above this
addendum describes the historical pip flow and remains accurate as
background; where they disagree, the uv flow wins.

Platform note (2026-10-25): the benchmark subsystem imports the Unix-only
`resource` module and is un-importable on Windows; the benchmark and
`tests/unit/test_benchmark*.py` / benchmark-related `test_residual_risk`
tests execute on Linux (dev platform and CI) only.
