# LibzentSuite — TODO + Ideas

**Reorganized 2026-11-13** to reflect the post-dashboard-removal,
post-Mudae-removal codebase. Everything here describes what the
repository **currently holds** — no stale references to removed systems.

---

## Core Design Goal

    One Core API (ControlAPI)
          |
      +---+---+
      |       |
     CLI     Future UI (FastAPI + Flet)

Both interfaces manipulate the exact same:
- Accounts and plugin assignments
- Plugin profiles (settings presets)
- Tasks and scheduling
- Rate-limit policies and the safety floor
- Credentials (encrypted at rest)
- Logs and audit trail
- Simulation system
- Fleet orchestration

Plugins remain independent from the interface layer.

---

## Current State (2026-11-13)

**What exists and is tested (1,673 tests green, CI green):**
- Core framework: bootstrap → ControlAPI → managers → repositories → adapters
- Multi-account Discord orchestration (discord.py-self, lazily imported)
- Plugin system: discovery, lifecycle hooks, settings, self-commands, per-account instances
- Plugin profiles: named settings presets, account-to-preset bindings
- AccountPluginData: per-channel operational data (own SQLite store, optimistic versioning)
- Sync groups: persistent membership + shared pacing clock
- Fleet orchestration: task lifecycle, handoff, cooldowns, reconciler, dispatch guard
- Rate limiting: 1.5 s safety floor, learned defaults, server-cooldown handling, send lane
- Credential service: Fernet-encrypted at rest, key outside DB, write-only in UI
- Parser registry: generic, empty, game-packs plug in via `register_parser()`
- CLI: REPL + one-shot commands (the only interface currently)
- Simulator: complete offline Discord replacement with scenarios
- Build: uv-locked, ruff-gated, mypy-advisory (1,086 baseline), CI on GitHub Actions

**What was removed (2026-11-13):**
- The entire dashboard interface (custom HTTP server, WebSocket, SPA, builder contract)
- The Mudae parser pack (~3,000 lines of game-specific recognition)
- All Mudae references (renamed to "primary bot" — game-agnostic)

---

## Active Queue

[ ] **FastAPI core API** — replace the deleted dashboard's REST surface
    with FastAPI routers calling the existing ControlAPI. Pydantic
    request/response models. Native WebSocket for live events.
    Study: `docs/pivot-audit-fastapi-flet.md` (phase 2)

[ ] **Flet UI** — replace the deleted SPA with a Python-native UI.
    Pages per module (accounts, fleet, orchestration, extensions,
    control plane, system). Plugin UI contribution contract redesign.
    Study: `docs/pivot-audit-fastapi-flet.md` (phase 3)

[ ] **Switch to better.py-self** — drop-in fork with memory caps,
    cache persistence (75 ms warm restarts), throttle events, latency
    EWMA, shared-scope 429 correctness. Study:
    `docs/better-py-self-integration.md`

[ ] **PHASE C LIVE DEBUG** — bravo re-credentialed and running; alpha
    still needs a fresh credential. Remaining: admin-command flow
    B -> A, ordered full walkthrough.

---

## Open Ideas (unscheduled)

### Fleet enhancement

[ ] Metric rollup wiring: declared metric hints (sum|last) surfaced
    through the future FastAPI dashboard — the opt-in GLOBAL view over
    AccountPluginData (the store + LOCAL view shipped; only the
    aggregation surface is missing)
[ ] cooldown_key policy mapping: TaskDefinition.cooldown_key read
    through to FleetPolicy so a task's pacing key can live in its
    per-channel data document (the field exists, the mapping is unwired)
[ ] FleetTaskHandler: the capability protocol is fully implemented and
    tested (33 fleet tests) — zero plugins currently use it; the first
    real fleet plugin validates the contract end-to-end

### Rate limiting (better.py-self integration)

[ ] Bridge `on_http_throttle` to `RateLimitManager.report_server_limit()`
    — the REAL throttle signal for user accounts (library absorbs 429s
    internally; we currently only see the ones it doesn't)
[ ] Add per-route latency EWMA congestion watch (`http_route_latencies()`)
    — queuing inflation, not 429s, is how the send ceiling manifests
[ ] Add bounded probe-down recovery (decay backoff ≤10% after 2 min clean,
    never below the base interval) — matches the fork's lane governor recipe
[ ] Expose `cache_stats()` through ControlAPI for the future dashboard

### v2 Architecture (design-only, no backend)

[ ] Workspaces: organizational boundary above accounts
[ ] Collections: flexible user-defined account groupings
[ ] Execution Plans / Schedules: higher-level orchestration above tasks
[ ] Workspace-scoped configuration (Global → Workspace → Account)

---

## Codebase Hygiene

[ ] mypy strict ratchet: 1,086 errors → 0 (fleet family first, then
    framework_ui, then dashboard_ui components, then api.py)
[ ] `core/remote_control.py` (365 lines): only the simulator's
    `control_mention` scenario uses it — delete when that scenario
    is retired

---

## Removed Systems (context for new contributors)

| System | Removed | Superseded by |
|---|---|---|
| ProfileManager (permission bundles) | 2026-10-22 | Direct account→plugin assignment (`account_plugins` table) |
| Mudae parser pack | 2026-11-13 | Generic parser registry (`parsers/manager.py`, ships empty) |
| Dashboard (custom HTTP/WS/SPA) | 2026-11-13 | FastAPI + Flet (planned) |
