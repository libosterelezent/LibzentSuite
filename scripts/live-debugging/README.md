# Live-debugging toolkit — the whole-suite LIVE harness

`live_debug.py` boots the **real** LibzentSuite stack — the exact
`core.bootstrap.build()` path `main.py` uses — with only the Discord
boundary replaced by the offline simulator (`adapters/simulator/`), then
drives **every feature area** in ordered phases while a parallel anomaly
window watches for anything that deviates from the documented contracts
(`docs/architecture/architecture.rst`, `docs/architecture/integration.rst`).

It is **not** a test suite. It is a supervised reconnaissance pass: a
human watches two visually separated feeds, and every glitch lands in a
structured findings file for triage afterwards.

## Safety contract

- **Never touches `state/`** — the live databases and the credentials
  key (AGENTS.md §7). Every boot happens inside a disposable
  `tempfile.mkdtemp` workdir (database, Fernet key, staged plugins),
  removed at exit. Use `--keep-workdir` to retain it for forensics.
- **No network, no real tokens.** All credentials are synthetic
  (`sim-token-*`) values; the simulated world is deterministic
  (seeded per account).
- **Read-only towards the repo**: the harness copies plugin sources
  into its workdir; nothing under `core/`, `adapters/`, `interfaces/`,
  `plugins/`, or `state/` is written.
- Every timing wait is bounded and announced (quiet windows ≤ 30 s,
  the reconciler sweep ≤ 45 s, one scenario ≤ 120 s, one phase ≤ 300 s).

## Running it

```bash
python3 scripts/live-debugging/live_debug.py --list          # the phase table
python3 scripts/live-debugging/live_debug.py                 # the full pass
python3 scripts/live-debugging/live_debug.py --phase cog-commands
python3 scripts/live-debugging/live_debug.py --phase boot --phase rate-limits
python3 scripts/live-debugging/live_debug.py --watch         # 2nd terminal
python3 scripts/live-debugging/live_debug.py --keep-workdir  # forensic mode
```

Exit code: `0` clean, `1` when any critical/major finding was recorded,
`2` usage error. A full pass takes roughly 2–3 minutes (the
chains-signals phase waits out one reconciler sweep; the simulation
phase runs two scenarios).

### The supervision contract (what you watch)

One console carries two visually distinct feeds:

- **Phase progress** — `--- phase: n/N <name> ---` banners, `step:`
  lines, and one `[PASS]`/`[FAIL]` line per expectation. This is the
  "everything works" stream.
- **The anomaly feed** — `[ANOMALY] <id> [<severity>] <summary>` lines,
  emitted the MOMENT the capture layer sees something: an ERROR-class
  structured log row, a contract-suspicious WARNING, an unhandled asyncio
  task exception (captured via the loop exception handler — never left
  to stderr), or a failed phase expectation.

Open a second terminal with `--watch` to tail the findings file live
(it prints each finding as it is appended and follows newer run files).

## What each phase covers

| # | Phase | Area | Exercises |
|---|-------|------|-----------|
| 1 | `boot` | framework | real bootstrap over the simulator, plugin discovery (production `ping` + the toolkit fixtures `ldp_worker`/`ldp_follower`), side-car isolation check (`plugin-data.db` inside the workdir), status snapshot, env-import verb, credential-leak scan |
| 2 | `accounts` | accounts | add (+Fernet credential), duplicate-add guard, start, `accounts_info`, account-scoped emergency stop (async by contract — bounded wait), stop/remove with cascade |
| 3 | `credentials` | credentials | encrypted store status, rotation, `mgmt:credential.*` audit rows (after the queued-persistence drain), env migration with overwrite, no-leak checks |
| 4 | `plugins` | plugin-assignment | assign/unassign/replace on a RUNNING account, typo trap, enable/disable through `plugins_state` |
| 5 | `profiles` | account-plugin-profiles | create/update/set-active/active/clear-active, effective-settings view |
| 6 | `cog-commands` | cog-dispatch | hosts `plugins/ping`'s cog on a real library `commands.Bot` via `plugins.host_cog` with the live paced facade, then drives `!ping <channel> <text>` through `Command.invoke` (typed converters, `ctx.services` lift mirrored from the adapter's `before_invoke`) — paced delivery, `ctx.send` reply, audit row, FlowBlocked path on a vanished channel |
| 7 | `tasks-scheduler` | scheduler | owner-triggered paced bursts, `tasks_history` statuses, mid-flight `tasks_cancel_matching` (cancelled rows persist, cancelled tail never sends) |
| 8 | `rate-limits` | pacing | effective policy at the 1.5 s floor, MEASURED inter-send gaps from audit timestamps, account-level override + clear, below-floor rejection, warnings/stats, cooldown list |
| 9 | `workspaces` | workspaces | create/join/pause (generation bump)/resume, eligibility, workspace cooldowns (start/list/clear), delete-with-members guard, leave |
| 10 | `presets` | workspace-presets | settings-template preset create/preview/apply (revision guard)/detach/stale-preview refusal; P8 reference-style definitions + execution start/checkpoint/resume |
| 11 | `chains-signals` | chains-signals | chain definition (terminal trigger on `TaskCompleted`), A-acts-B-follows run + step execution, durable signal emission through the outbox, reconciler delivery (bounded wait over one 30 s sweep) firing the signal-triggered chain |
| 12 | `observability` | audit-observability | account-filtered / global (`global-log`) / severity-filtered log tails, log stats, audit history incl. mgmt rows, bus lifecycle event rows |
| 13 | `simulation` | simulator-scenarios | the `simulation_list` facade verb; `export_basic` at the CURRENT `--sim` default (the workload fixtures), plus an explicit production-dir `--plugins` override case that must fail honestly (no workload there) |
| 14 | `shutdown` | framework | graceful ordered teardown with a no-new-anomalies check and a zero-failure cleanup check (`Framework.shutdown_failures` must be empty) |

The toolkit's fixture plugins (staged beside the real `plugins/`
inside the workdir) provide the traffic. They are split by ROLE (PR
review 2026-11-18 — one plugin playing both roles self-perpetuated
runs: the follower's sends re-matched ldp_worker's own terminal
trigger, and its handler re-emitted the `chain-done` it was started
by):

- `plugins/ldp_worker/` — the ACTOR: an owner trigger word
  (`ldp-go [n]`) that paced-bursts through the pipeline, plus the
  `chain-done` durable signal through the facade's `emit_signal`.
- `plugins/ldp_follower/` — the FOLLOWER: the conventional
  `on_ldp_follower_trigger` chain-step handler (one paced send, no
  emission, no chat surface — the follower account cannot react to
  the actor's trigger word structurally).

## The findings file

`findings/run-<stamp>.jsonl` — one JSON object per anomaly, appended
live (anomalies ONLY; PASS lines go to the run summary):

```json
{"id": "20261117T205733-0003", "ts": 1763398021.5,
 "phase": "accounts", "area": "accounts", "severity": "major",
 "summary": "step 'remove gamma (cascade)' raised AccountError: ...",
 "evidence": ["<log excerpt lines>"],
 "expectation": "the documented contract", "actual": "what happened",
 "repro": "accounts/remove gamma (cascade)"}
```

- `severity` — `critical` (crashed phase / unhandled task exception /
  budget overrun), `major` (ERROR log row, failed expectation, raised
  step), `minor` (contract-suspicious WARNING), `observation`
  (noteworthy but documented/known-benign, e.g. the deliberate ERROR
  line the account manager logs on emergency stop).
- `repro` — the phase + step name; rerun with
  `--phase <phase>` to reproduce in isolation.
- `findings/run-<stamp>.summary.json` — per-phase PASS/FAIL step
  counts, durations, findings-by-severity, exit code.

## How the anomaly capture works

- `LogService.subscribe` — every structured row is classified
  (`classify_log_entry`): ERROR → major unless it matches a
  known-deliberate pattern; WARNING → minor when the message reads like
  a contract violation, observation when it matches known-benign sim
  behavior; INFO/DEBUG are never findings.
- `AuditRecorder.subscribe` — a bounded audit tail (last 48 rows)
  provides evidence context attached to each finding.
- `loop.set_exception_handler` — "Task exception was never retrieved"
  wreckage becomes a critical finding instead of stderr noise; the
  previous handler is restored on teardown.
- Phase steps — every step names its expectation; a mismatch or an
  unexpected raise is a finding with expectation/actual/repro.
  Expected-negative steps (guards, typo traps) use `step_raises` and
  PASS when the guard fires.

## Observation gaps (framework changes NOT made — by design)

The toolkit is an observer; these gaps are recorded here instead of
patching the framework:

1. **Chains have no facade verbs.** `ChainRuntime.create_chain` is
   bootstrap-local; creating a chain required writing a
   `workspace_chains` row through the repository. Manageability from
   the CLI/API is a product decision for the owner.
2. **P8 preset definitions + executions are service-level only**
   (`preset_define`, `preset_apply_references`, `execution_*` have no
   ControlAPI verbs — only the settings-template preset path does).
3. **The workspace reconciler has no status/heartbeat.** Its 30 s loop
   is invisible to logs and status pages; the harness verifies it only
   behaviorally (signal delivery + chain runs).
4. **Workspace service events publish to a debug logger** (bootstrap
   wires `publisher=ROOT_LOGGER.debug`), so membership/pause
   transitions never reach the structured log tail.
5. **Cog hosting is invisible over the simulator.** The reconcile path
   looks for the adapter's library Bot on `runtime.client._client`;
   the sim client has none, so cogs never host on sim boots. The
   harness hosts the cog itself to exercise the command face.
6. **`--sim` with an explicit production `--plugins` override finds no
   workload.** `main.py --sim` now defaults to
   `tests/fixtures/sim_plugins` (where the exporter workload lives), but
   pointing `--plugins plugins` at the production dir still leaves every
   scenario without its workload. The `simulation` phase runs the
   default positively and pins the override case to failing HONESTLY
   (send failures reported, never a vacuous pass).
7. **Cancelling an in-flight plugin send surfaces as a plugin-crash
   ERROR row** — tasks_cancel_matching aborts the queued task and the
   plugin await re-raises SchedulerError inside handle_event, which the
   isolation boundary logs as a plugin crash. Captured as a finding on
   every run; triage whether the task-ack seam should deliver
   cancellation as a distinct, catchable signal instead.

## Files

- `live_debug.py` — the runner (phases, capture, findings, watch).
- `plugins/ldp_worker/` — the toolkit's ACTOR fixture (paced bursts,
  the `chain-done` facade emission).
- `plugins/ldp_follower/` — the toolkit's FOLLOWER fixture (chain-step
  handler: one paced send, no emission, no chat surface).
- `findings/` — output (gitignored).
