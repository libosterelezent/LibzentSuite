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
- Two-level data model adopted: account-owned plugin profiles (plugin
  defined schema, channels-in-profile, profile-scoped cooldowns/tasks)
  coordinated by workspace presets that reference — never copy —
  profiles (`docs/design/data-management-adoption.md`).
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
