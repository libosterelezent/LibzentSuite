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
