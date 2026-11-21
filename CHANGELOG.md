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

### Fixed
- PR #27 review round 7 (2026-11-18), all nine findings:
  `ControlAPI.plugins_set_enabled` now validates the account and
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
