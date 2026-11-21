# LibzentSuite — Roadmap

The active queue and the historical record of what shipped. Design
decisions live alongside in `docs/design/`; binding contracts in
`docs/architecture/architecture.rst` + `architecture/integration.md`;
deep studies under `docs/plans/`. Completed items keep their summaries
in `docs/reference/completed.rst` and their full detail in the local
`knowledgebase/` (AGENTS.md rule 8).

---

## Core Design Goal

    One Core API (ControlAPI)
          |
      +---+---+
      |       |
      CLI     FastAPI + Flet (the interface pivot)

Both interfaces manipulate the same: accounts + plugin assignments,
profiles (plugin-owned) + workspace presets (coordination), tasks and
scheduling, rate-limit policies + the safety floor, credentials
(encrypted at rest), logs and audit, simulation, workspace
orchestration. Plugins stay independent of the interface layer.

## Current State (2026-11-21)

**1,678 tests green, CI green, pyright ZERO** (equality ratchet at
`.github/pyright-baseline`). pytest + xdist owns the runner (`-n
auto --dist loadscope` — each gate leg ~3× faster); mypy retired
with both its trees at ZERO. The two-level data model is live
(profiles + presets +
executions, migrations v12–v16). The FastAPI core API and the Flet UI
shell are landed with honest dependency extras (`[api]`, `[ui]`,
`[all]`) — the wiring is the remaining connective work. Full
user-facing historical record: `docs/reference/completed.rst`.

---

## Active Queue

*In priority order. The engine, the data model, and every quality
gate are done — what remains is connective: wiring the interfaces,
then shipping the first release.*

### 1 — The Flet UI (the real interface)

The spec is the owner's contract: `docs/plans/flet_ui_design_contract.md`
(strict ownership rules, FastAPI boundary, native-Flet-first component
architecture, page registration). The pages it calls for: Overview,
Accounts, Workspaces, Plugins, **Account plugin profiles**, **Workspace
presets**, Tasks, Logs/Audit. Today's `interfaces/ui/` is a four-page
shell (status/accounts/workspaces/logs) — this replaces it with the
contract's coherent operational control surface.

### 2 — FastAPI wiring (serve the API)

`build_app` exists and is tested; nothing boots it. `main.py` gains a
serve mode (uvicorn, the `[api]` extra now in the lock), the token
seam surfaces via env/config, and the health/status routes get their
first live consumer (the Flet shell's ApiClient already points at the
right URL shape).

### 3 — WS event bridge (live events)

`/api/ws` is a 2s status-poll placeholder. The typed EventBus exists —
bridge it: subscribe per-connection, forward account/plugin/task events
as JSON frames, backpressure-bounded. The Flet pages want live
updates, not polling.

### 4 — Release enablement (when you say go)

Lift the `v*` tag ruleset hold, cut `v0.1.0` through the pre-built
pipeline (build from the exact tag, CHANGELOG notes, artifacts). The
structure is tested and waiting.

---

## Open Ideas (unscheduled)

[ ] Framework control from accounts — the replacement method for the
    retired chat verbs (admin/prefix/help management) — owner design
    pending (removed 2026-11-17; the CLI/API own it meanwhile).
[ ] Metric rollup wiring — the opt-in global view over
    AccountPluginData (store + local view shipped; aggregation missing).

---

## Removed / Superseded Systems

| System | Removed | Superseded by |
|---|---|---|
| ProfileManager (permission bundles) | 2026-10-22 | Direct `account_plugins` assignment |
| Mudae parser pack | 2026-11-13 | Generic parser registry (ships empty) |
| Dashboard (custom HTTP/WS/SPA) | 2026-11-13 | FastAPI + Flet |
| Sync groups + fleet | 2026-11-14 | Workspace unification (`core/workspace/`) |
| COMMANDS/CommandSpec/ArgSpec facade | 2026-11-15 | Cog declarations (the convergence) |
| RemoteControl mention bridge + hub machinery | 2026-11-15 | The retirement wing (`dump/`) |
| Legacy template profiles | 2026-11-16 | Account-owned instances (the v15 fold) |
