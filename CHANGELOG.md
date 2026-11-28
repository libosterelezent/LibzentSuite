# Changelog

All notable changes to LibzentSuite are documented here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
follows semantic versioning once releases begin (the version is a
placeholder until then).

When cutting a release, the build workflow extracts the matching
section from this file as the GitHub release notes — a tag without its
section here falls back to GitHub's generated notes.

No releases are cut yet — deliberately. The release pipeline is
pre-ready but unarmed (the `v*` tag ruleset grants no bypass), so
nothing can ship until that hold is lifted; see
`.github/workflows/build.yml`.

## [Unreleased]

### Added
- The `expect` primitive (client-capability Track B): a send-coupled
  reactive window on the plugin services surface —
  `async with ctx.services.expect(check, timeout) as window:` enters
  the subscription BEFORE the send runs inside the block, so a reply
  landing during the send's queue wait matches exactly like one
  landing after (the race a `wait_for` issued after the send loses,
  closed by construction; the framework owns and unwires the waiter).
  Await surface: `window.match()` (first match, `None` on deadline),
  `window.collect(n)` (up to `n`, partial on deadline),
  `window.first_of(*checks)` (any-check match over the same buffer).
  Checks see the `MessageReceived` event — the `.message` rich model is
  frozen, the event is shared; raising checks surface
  as `AccountError`; `wait_for_message`/`collect_messages` now share
  the window's subscription seam (`_subscribe_waits`).
- Anti-flake tooling for the test suite: every test now carries a
  120 s per-test hang ceiling (`pytest-timeout`, thread method,
  configured in `pyproject.toml` — a hang fails by name with every
  thread's stack dumped, instead of a leg dying at its ceiling with no
  attribution); `wait_until_async` lands in `tests/unit/helpers.py` as
  the canonical observable-poll wait; `pytest-rerunfailures` joins the
  dev group for flake hunts (runner-side use only — the gate never
  auto-reruns a failure).
- The push protocol is machine-enforced: `scripts/local-ci.sh`
  self-installs a `pre-push` hook and, on its green path, stamps the
  tree the run STARTED with — written only when the index and
  worktree still match it at the end, so a mid-run stage cannot
  name an untested tree (the gate also fails fast on unstaged
  tracked changes); the hook reads every update git feeds it and
  refuses any push whose OUTGOING tree has no
  matching stamp. AGENTS.md 10 tightened to match - no
  equivalent-evidence substitutes for the gate, no
  authorizations carried over from plans or summaries.

### Removed
- The framework-level primary-bot id: `AccessManager.primary_bot_id`/`set_primary_bot_id`, the `Credentials.primary_bot_id` env fallback, the `admin_primary_bot_id*` API verbs, and the `PRIMARY_BOT_ID` env key. The suite is game-agnostic — a target-bot id is plugin-schema domain now (the exporter sim fixture declares its own `bot_id` setting). Migration v17 folds the stored `access:primary_bot_id` value into a `access:primary_bot_id.retired` archive row, then deletes the live key — the operator's configured id stays recoverable, not destroyed.

### Changed
- The test runner is pytest + xdist (`-n auto --dist loadscope`);
  `unittest discover` retired — each CI/gate leg drops from ~7.5
  minutes to ~2m45s. pyright (basic mode, 3.10 target) owns static
  typing with the equality ratchet at `.github/pyright-baseline`
  (opened at 72, marched to 0); mypy retired from the dev group and
  the lock. `scripts/local-ci.sh` is rewritten as the clean
  seven-step code-quality gate — the CodeRabbit machinery left the
  gate entirely (it reviews PRs on GitHub, not local runs).
- The project is platform-independent. No platform-only modules remain:
  the benchmark sampler and the DB worker-thread telemetry measure CPU,
  memory and handles with a stdlib clock plus the optional `psutil` (dev
  group), falling back to a plain `/proc` read on Linux and reporting
  memory as unavailable — never zero — where neither can observe the
  process. The suite is green on native Windows (seven capability-bound
  tests skip with their reason); the local gate and `scripts/run_tests.sh`
  run from Git Bash. CI runs the suite on Windows (the owner's
  platform) on 3.10; the local gate proves the same suite on any
  machine, Linux included. The duplicated pyright step is gone, and the
  step now parses
  its count with the project's own interpreter.
- The local gate runs ONE suite leg (3.10 + coverage) instead of
  two (3.10 plain; 3.13 + coverage): CI runs 3.10 only and never
  ran the 3.13 leg — duplicated effort costing ~10 minutes per
  gate run. The coverage floor is now measured on the 3.10 leg,
  and the local `.venv` matches the gate interpreter.
- The push protocol swaps its enforcement point: the local
  full-suite gate (`scripts/local-ci.sh` + the stamp/hook
  mechanism) retires; `scripts/preflight.sh` (ruff + pyright,
- The Python source folds into a single libzentsuite/ package:
  every import becomes libzentsuite.core.*, libzentsuite.adapters.*,
  etc. — the wheel ships collision-free names instead of the flat core,
  dapters, database top-level packages that could collide with any
  other package. main.py becomes libzentsuite/__main__.py with a
  [project.scripts] console entry point (python -m libzentsuite and
  libzentsuite both work). The 9-item PROD list in every config
  (ruff, pyright, preflight, CI, coverage) collapses to one name.
  plugins/ stays at the repository root (runtime-discovered, not a
  package). 142 files rewritten, 702 import lines changed.
  seconds) runs before every commit, and the CI runner is the
  merge prerequisite. Every PR carries its tags and the body
  format (`.github/PULL_REQUEST_TEMPLATE.md`): Summary, Testing,
  Pre-PR review, General Info — the body is the certification
  narrative, honestly stating what was verified and what was
  NOT re-verified. The swap's evidence: the local gate produced
  ~50% environmental false positives under machine load while
  the isolated runner caught the one real bug the gate missed
  (the float cancellation). The runner is the better oracle.

### Fixed
- Two timing flakes the first Windows CI run of the portability branch
  exposed: the quiet-sender collector raced its own 0.05 s idle timeout
  against a 0.02 s staging sleep (now 0.5 s vs 0.1 s — 5x margin), and
  the PerfCollector p50 assertion hit Windows timer quantization (a
  10 ms sleep measured 3.8 ms on the ~15.6 ms tick grid — now a 50 ms
  sleep with a 20 ms floor, which undershoot cannot cross).
- The identify-gate timing family gets the margin medicine: the wave
  budget's slack covers the 0.232s of scheduler lateness a 4-worker
  gate run produces (0.20 was 32ms short), the depth projection fires
  under any arrival staggering a loaded loop produces (max_wait
  0.45 to 0.20 - at 0.45 the queue never reached projection depth and
  zero waiters failed), and the pacing sampler's second honesty limit
  ("read no clock" under a stalled stats call) joins the re-measurement
  path alongside "cannot be judged". Riding along: the exporter
  fixture's plugin-owned bot_id guard (PR #33's review finding, parked
  through the merge).
- The teardown takeover race (reproduced deterministically,
  2026-11-25): a second teardown waiting on an in-flight one
  could observe an already-cancelled owner as still live when
  its patience expired in the same event-loop batch as the
  (`on_unload`) ran nowhere. The waiter now waits for
  EITHER outcome - the completion event or the owner task, whichever
  first (a prompt takeover), then drains
  one loop turn before re-checking: the drain runs every
  already-queued step first — including the owner's unwind — so
  a decided abandonment is never misread as a live owner, and a
  genuinely parked owner still bounds the wait. Regression test
  included (coalesced-deadline coercion; fails on 3.12+ pre-fix).
- The restart-matrix cooldown assertion bounded the whole
  restart sequence at 5.000s of wall time — a loaded Windows
  runner took 5.014s (294.9859 against a 295.0 floor). The
  bound now tracks measured elapsed (300 — elapsed — 0.25s),
  which is what the test actually means.
- The pacing sampler's honesty limit is a re-measurement, not a red
  gate: "pacing cannot be judged for this run" fires when a poll lands
  more than an interval late (multi-second callback stalls on a busy
  desktop, observed in-gate 2026-11-25) — a limit of the MEASUREMENT,
  not a verdict about pacing. `Stack.pacing_stamps` gained a `first`
  window offset and `pacing_stamps_with_retry` re-triggers the flow
  and measures the fresh window (a real spacing violation never takes
  that path); both pacing tests use it.
- Portability defects the first native-Windows run exposed: a hard
  `import resource` made the whole benchmark package unimportable;
  exception locations were built with a `/` split that returns the whole
  path on Windows; three tests hardcoded a `/tmp` key path; a test-side
  directory fingerprint keyed on OS separators; the gate parsed its
  pyright count with the system's `python3` (on Windows often the Store
  stub) and a `/tmp` path embedded in a Python literal;
  `scripts/run_tests.sh` still invoked the retired `unittest discover`
  and only knew the POSIX virtualenv layout.
- Three telemetry tests closed their database only after the temp
  directory had been removed. POSIX forgave the unlink of an open file;
  Windows refused the delete. Databases now close inside the scope that
  removes them.
- The scheduler unit tests slept for a fixed 10-500ms and then asserted
  a state. On a loaded event loop (the 3.10 leg under four xdist
  workers on a four-thread Windows machine) a task was still `queued`
  when the 50ms sleep ended: five failed in the gate, eleven distinct
  ones across three deliberately starved rounds. Every wait is now on
  the observable — the published terminal event, the recorded acquire
  call, a set event — and those rounds pass on 3.10 and 3.13.
- The full-stack pacing tests prove spacing on the pacer's own
  spacing-clock stamps (`ratelimit_stats`), which stay a full interval
  apart under any load, and that no send reached the wire before its
  stamp. No fixed floor is asked of a measured wire gap any more: the
  pacer stamps at the expected wire instant, so a send that load delays
  shortens the next gap by exactly its lateness (one send running 110ms
  later than the next left a 1.3896s gap under the gate's coverage leg).
  Regressing the pacer to 1.42s on purpose now fails both tests at the
  stamp assertion; the exact interval itself is pinned by the engine's
  unit tests under an injected clock. A sampler timeout carries the
  framework's newest log lines.
- The exporter fixture's DM idle window in the full-stack tests is 3.0s,
  the value the simulation scenarios give it. At 0.6s the gate's
  coverage leg collected no pages and the flow sent no chunk.
- `test_rich_model`'s pipeline test read the audit rows the moment the
  task statuses flipped; the rows are written in the finish tail, after
  that. It waits for the published terminal events now.
- The simulator's `pacing_respected` scenario measured wire gaps against
  a 100ms budget and failed the gate's coverage leg on a healthy pacer
  (1.473s for a 1.6s interval: one send 127ms late). Lateness is part of
  a measured gap, so the budget is 300ms now, and the check is described
  as what it shows (pacing in force; a bypass collapses the gaps) rather
  than as the hard floor on the wire.
- The CI-workflow guard against a bare `python3` missed the heredoc
  and backtick forms, and no test checked that it could catch
  anything. It now recognises the word in any shell position and has
  a test of its own. A `#` inside quotes is no longer taken for a
  comment, and an escaped quote no longer opens a string: both
  `echo '#'; python3 -m pytest` and `echo \" " # "; python3 x` used to
  pass the guard.
- The README gave only cmd's `set UV_PYTHON=3.13` for pinning the
  interpreter. In bash `set` assigns a positional parameter and exports
  nothing, so Git Bash users got no pin. The `export` form comes first
  now, with the cmd and PowerShell forms labelled.
- The full-stack pacing sampler records the limiter's own clock
  readings and rebuilds each stamp from the very one that produced its
  age. Reading the clocks around the call, as it first did, let a thread
  the OS descheduled in between move a stamp: later, and a healthy send
  failed; earlier, and a send that left early passed.
- The pacing sampler refuses to record a stamp that does not advance:
  two sends matched to one grant mean the sampler fell behind, and the
  failure says that instead of reporting a zero gap that looked like a
  pacing violation.
- The workload test waits for the TaskCompleted event itself before
  reading the log tail. The audit rows it waited for are written before
  the event is published, so the assertion could race.
- PR #27 review round 7 (2026-11-18), all nine findings:
  `ControlAPI.plugins_set_enabled` now validates the account and

- Dead code removed: the `[tool.mypy]` section in `pyproject.toml`
  (mypy was retired from the dev group and the lock; the config
  described a CI step that no longer runs) and two stale `__all__`
  names in `interfaces/cli/__init__.py` (`format_group_snapshot`,
  `format_groups` — functions that no longer exist in
  `commands.py`).
  plugin BEFORE any state moves (an unknown account used to get a
  success echo plus an audit row — reconcile was silently no-op), and
  its `surface` default matches every sibling mutation (`"api"`);
  `AccountManager.reconcile_plugin_cogs` fails closed on unknown
  accounts and takes back cogs it freshly hosted when a `stop()`
  lands mid-pass (the pre-await bot could leave a cog attached to a
  disconnected client); `Framework.shutdown()` records cleanup
  failures on `shutdown_failures` (the warnings died with the
  detaching log bridge — a failing `database.close()` still read as a
  graceful shutdown); the live-debug harness passes `db_path` so the
  plugin-data side-car and the credential key stay in its disposable
  workdir instead of the live `state/` (the same gap in the
  integration/hardening test boots is closed with it); the harness's
  `follow-burst` chain definition carries its required `name` and is
  validated through `ChainRuntime.parse_definition` before persisting
  (a nameless row could never match its trigger and blocked signal
  intake workspace-wide); the chain probes' actor/follower roles are
  split into `ldp_worker`/`ldp_follower` fixture plugins (the
  follower's sends re-matched ldp_worker's own terminal trigger and
  its handler re-emitted `chain-done` — the probe self-perpetuated
  runs); `scripts/local-ci.sh` parses `--review` anywhere on the
  command line (it was only checked as `$2`, so
  `./local-ci.sh --review` skipped the review while still declaring
  "safe to push") and scopes the persistent Python 3.10 venv to the
  checkout with an flock through the 3.10 leg (two checkouts with
  different lockfiles used to mutate one shared environment under
  each other's running tests).
- The pre-push review over the pending range (2026-11-19, 8 findings,
  6 fixed / 1 resolved by the gate restructure / 1 triaged): a
  cancelled `Framework.shutdown()` owner now FINISHES joining the
  terminal-trigger reap before its cancellation propagates (the bare
  shield surfaced the Cancel immediately while the detached reap kept
  unwinding against dependencies shutdown was already closing);
  standalone live-debug phases report `shutdown_failures` from their
  implicit teardown (a failed `database.close()` could still exit 0
  without the shutdown phase); the gate resolves `coderabbit` from
  PATH before the installer default, syncs the persistent 3.10 venv
  `--exact` (dropped dependencies no longer linger in it), and parses
  the review stream as NDJSON (the literal grep missed spaced JSON,
  an empty stream read as "0 findings", and a skipped review now
  fails); two scheduler tests traded wall-clock settles for
  event-proven park points — the 3.10 leg failed the torn-tail
  regression under distributed load because a 20ms sleep let the
  stop's abort legitimately cancel a task whose completion tail had
  not parked yet; the gate's 3.13 leg is concurrent-gate safe —
  coverage data lives under `COVERAGE_FILE` in the per-run scratch
  dir and the leg holds the checkout flock (two gates used to clobber
  the one repo-root `.coverage`, and `rm -f .coverage.*` could delete
  the other's in-flight worker data — a clobbered read measured 0.0%
  and failed the floor). A second fresh review over the grown range
  (19 findings: 12 fixed, 7 triaged) hardened the same surfaces: the
  3.13 suite leg measures its xdist workers through pytest-cov (plain
  parallel-mode coverage saw only the orchestrating master — 0.0%
  against the floor, CI included); the harness's estop-echo downgrade
  is phase-scoped (an ERROR-class disconnect nothing requested is a
  major again, not an observation); failed scan steps no longer print
  PASS; the cancel probe compares verb count to persisted rows by
  equality; teardown restores the loop handler only if it installed
  one; an interrupted simulator subprocess is killed and reaped; the
  trigger word matches at a token boundary; the live window
  unsubscribes before its shielded reap; a second cancellation cannot
  cancel the trigger-reap join; and the gate treats every
  non-`review_completed` terminal review status as a failure.
- PR #27 round 9 (the post-push PR comments, 2026-11-21): the cog
  reconcile rechecks the live enabled flag around each host — a
  disable racing the `add_cog` await was invisible to the disable's
  own reconcile pass (attach records the cog only after `add_cog`
  returns), leaving the plugin's commands invocable; the live-debug
  harness clears an ambient `LZ_MASTER_KEY` for the run (an operator
  key broke the workdir key-sidecar expectation and encrypted
  synthetic tokens under real key material); the gate creates the
  lock parent directory before the flock redirection, runs the
  concurrent review in its own session with the checkout-lock fd
  closed (a stalled review's surviving child used to hold the next
  gate's lock), signals the whole review process group on timeout,
  and survives the stall branch with `REVIEW_EXIT` initialized; the
  scheduler pressure probe waits for lanes blocked at the limiter
  instead of a depth poll that returned instantly; and the pytest
  configuration comment no longer describes the retired serial
  `test_rich_model` fallback.
- The round-9 gate's converged review: the terminal-trigger reap's
  NORMAL join is bounded too (the 5s budget covered only the
  cancelled-caller branch — a trigger cleanup that ignored its
  cancellation pinned shutdown's first step before
  `database.close()` ever ran); a completed review carrying
  `unreviewedFileCount` or a failed `outcome` fails the gate instead
  of reading "0 findings"; and the estop-echo downgrade is scoped to
  the account the harness named (`mark_disconnect_expected`) — a
  wrong account's ERROR-class disconnect during the accounts phase
  is a major again, not an observation.
  Triaged: the CLI live-log window's synchronous
  `print` can block the loop on a stalled consumer pipe (pre-existing,
  interactive `--live` feature — bounded off-loop output is a design
  change deferred to its own pass); the fail-closed toggle test's
  empty audit read is safe by construction (validation precedes the
  audit call, so the rejection path queues no row).

### Changed
- Implemented contracts moved from `docs/plans/` into the binding
  architecture document (rev 4.0): the two-level data model (new §4a),
  the better.py-self fork adoption contract (new §5a), and the FULL
  W0 workspace spec — the 18 locked decisions now live in §4's
  decision record (superseded preset semantics annotated), the W0
  spec file retired with it. The architecture doc's stale
  facts corrected in the same pass: the COG command face replaces the
  retired COMMANDS grammar in §5, presets REFERENCE profiles (never
  copy), the module map gains the landed API/UI interfaces and the
  cog host, migrations are v16. The absorbed plan files are RETIRED
  from `docs/plans/` — which now holds planned and in-progress
  contracts only (implemented structure in `docs/architecture/`,
  history in `docs/reference/completed.rst`).

### Removed
- The framework verbs no longer ride Discord accounts: the
  per-account command surface is exactly the attached plugins' COGs
  (help lists what each account actually runs). Framework control
  from chat — the admin/prefix verbs — is retired pending a
  replacement design; the CLI and API surfaces own framework
  management meanwhile.

### Added
- `scripts/live-debugging/` — the LIVE whole-suite debugging harness:
  boots the REAL stack (the `main.py` bootstrap path) over the offline
  simulator and drives every feature area in 14 supervised phases
  (accounts, credentials, plugin assignment, profiles, COG dispatch,
  tasks/scheduler, pacing, workspaces, presets, chains/signals,
  observability, simulation, shutdown) while a parallel anomaly window
  classifies log rows, tails the audit, and traps unhandled asyncio
  exceptions into a structured JSONL findings file (`--watch` tails it
  live; PASS lines go to a run summary). Never touches `state/` — it
  boots inside a disposable temp workdir with synthetic credentials.
  First full passes found: `main.py --sim` scenarios broken (the
  workload moved to `tests/fixtures/sim_plugins`, the runner still
  points at `plugins/`), terminal-triggered chains dead in production
  (TaskCompleted/TaskFailed carry no `action`, so the runtime's
  task_action match can never fire), and durable signal delivery
  unwired (the outbox is built with `deliver=None` and nothing wires
  the chain runtime in — signals pend forever).
- The plugin COG seam: plugin cogs get `ctx.services` — the
  per-(account, plugin) paced facade, bound by the cog host at attach
  time and lifted onto the command context by the client. Ping's
  command face (`<prefix>ping <channel_id> <text...>`) lands as the
  first real plugin COG.
- The two-level data model IMPLEMENTED (all eight phases, migrations
  v12–v16): account-owned plugin profiles — identity (account, plugin,
  name), profile-scoped cooldowns and task attribution, the
  plugin-facing lifecycle facade (zero scope parameters: isolation by
  construction), REST + CLI surfaces, the legacy fold (template
  bindings became instances, idempotent), and workspace presets that
  REFERENCE profiles (apply activates in place, never copies) with
  executions that checkpoint and resume without replaying (the
  contract: `docs/architecture/architecture.rst` §4a).
- FastAPI core API (`interfaces/api/`) and Flet UI shell
  (`interfaces/ui/`) over the one ControlAPI facade.
- Repository health stack: security workflow (gitleaks full-history,
  pip-audit against the exact lock, bandit), weekly health sweep
  (fresh-runner full gate, build sanity, action-pin validation,
  dependency-freshness probe → tracking issue), per-push build gate,
  release pipeline for `v*` tags, Dependabot (github-actions), native
  secret scanning + push protection, CodeQL, CODEOWNERS, SECURITY.md,
  and branch/tag rulesets.
- mypy ratchet gate with a checked-in baseline — now at **zero**:
  strict mode is a hard floor across all 108 source files.

### Changed
- Command convergence: plugins declare a real library `Cog` (COG on
  PluginBase); the COMMANDS/CommandSpec facade, manual tokenization,
  and the hub's registration path retired; all 15 framework verbs ride
  FrameworkVerbsCog.
- Workspace unification: one workspace concept (membership, pacing
  clocks, chains, durable signals) replaced sync groups + fleet.
- better.py-self fork fully adopted (memory-capped caches, per-account
  persistence, dynamic prefixes, authorization gate before parsing).

### Fixed
- Four latent crashes surfaced by the mypy-strict campaign: the
  `/api/plugins` route and the framework `pg`/`ratelimit`/`plugins`
  verbs called ControlAPI methods that never existed;
  `dashboard_ui_page`'s missing `_ui_account_context`; and the
  bootstrap `to_rich` seam passing bare `convert_message` (missing
  required `dm`) — TypeError on every call.

### Removed
- Dashboard (custom HTTP/WS/SPA), Mudae parser pack, sync groups +
  fleet, and the soulcopy naming (now `exporter`).
- The retirement sweep (2026-11-15): the RemoteControl mention bridge
  (+ the sim's control_mention scenario and the REMOTE_CONTROL gate),
  the hub command machinery (spec table, executor chain, PacingBackend,
  runtime_stats), plugin_base's hub-internal command grammar, the
  dashboard-era UI surface on ControlAPI, the never-constructed intake
  mixin, and the dead `serve_dashboard.sh` launcher — everything
  preserved verbatim under `dump/` with a full inventory.
### Fixed
- The offline simulator entrypoint crashed on every invocation (stale
  `args.dashboard` read predating the dashboard removal).

### Security
- Credentials encrypted at rest (Fernet); workflow tokens read-only;
  every CI action pinned via `actions.lock`; security policy with
  private vulnerability reporting.
