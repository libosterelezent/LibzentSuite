# LibzentSuite — Roadmap

The active queue and the historical record of what shipped. Design
decisions live alongside in `docs/design/`; binding contracts in
`docs/architecture/architecture.md` + `integration.md`; deep study
documents in `plans/` at the repo root. Completed items keep their
summaries here and their full detail in `knowledgebase/` (AGENTS.md
rule 8).

---

## Core Design Goal

    One Core API (ControlAPI)
          |
      +---+---+
      |       |
      CLI     Future UI (FastAPI + Flet)

Both interfaces manipulate the same: accounts + plugin assignments,
profiles (plugin-owned) + workspace presets (coordination), tasks and
scheduling, rate-limit policies + the safety floor, credentials
(encrypted at rest), logs and audit, simulation, workspace
orchestration. Plugins stay independent of the interface layer.

## Current State (2026-11-15)

**1,620 tests green, CI green.** Core framework; workspace
orchestration (chains, durable signals, membership); plugin system
with COG declarations + EMITS/FOLLOWS; command convergence complete
(cogs are the only command model); FastAPI core API + Flet UI shell
landed; better.py-self fully adopted. Rate limiting (1.5 s floor);
Fernet-encrypted credentials; CLI; full offline simulator; uv-locked,
ruff-gated, mypy-ratcheted.

Removed: dashboard (2026-11-13), Mudae parser pack (2026-11-13),
sync groups + fleet (2026-11-14), COMMANDS/CommandSpec/ArgSpec facade
(2026-11-15 — cogs replaced it).

---

## DONE — Historical Records

*These items are complete. Their detail lives in the knowledgebase and
the linked commit ranges. This section keeps only the summary.*

### Workspace Unification (W0–W5, 2026-11-13 → 2026-11-14)

ONE concept replaced SyncGroupManager + the fleet module: workspaces
own membership, pacing clocks, chains ("A acts → B follows,
visibly"), and the orchestration correctness core. Commits
`0332ed4`..`cb70a63`. Decision record: `WORKSPACE_DESIGN.md`.
Historical record: `knowledgebase/WORKSPACE_SUPERSESSION.md`.

### Command Convergence (C1–C4, 2026-11-14 → 2026-11-15)

Plugins ARE cogs for their command face. The COMMANDS/CommandSpec/
ArgSpec facade, manual tokenization, custom help, and the 922-line
SelfCommandHub bridge retired. `FrameworkVerbsCog` carries all 15
framework verbs. The hub keeps only bind/unbind + api_provider +
access wiring. Commits `415eab3`..`62b7fc7`. Historical record:
`knowledgebase/DISCORD_ADAPTER_FORK_INTEGRATION.md`.

### better.py-self adoption (complete)

Dependency switch, memory caps + cache persistence, `bot.add_check()`
gate, dynamic callable prefixes, throttle bridge, EWMA constants,
probe-down recipe, `cache_stats()` via ControlAPI. Commits
`8637d0a`, `9f2321b`, `06cec37`, `252aa78`, `838a0a1`.

### FastAPI core API (2026-11-14)

`interfaces/api/` — routers over ControlAPI: Pydantic models,
X-API-Token auth, the full surface, WebSocket scaffold. Commit
`ad417cb`.

### Flet UI shell (2026-11-14)

`interfaces/ui/` — page registry, ApiClient, NavigationRail shell,
four default pages. Commit `b713195`.

---

## Active Queue

### NEXT — Plugin Profiles + Workspace Presets (P1–P8)

**The two-level data model** (owner-locked 2026-11-15, full plan:
`adoption_of_data_management.md`):

- **Account plugin profiles** — each plugin defines its own profile
  schema, channels, cooldowns, and execution lifecycle. Identity:
  `(account, plugin, profile_name)`. The core persists + isolates but
  never interprets plugin-specific fields.
- **Workspace presets** — coordinate multiple accounts as a group.
  They REFERENCE profiles (never copy), own workflow state, execution
  dependencies, and cross-account synchronization. A workspace preset
  is a reusable orchestration definition; a workspace execution is a
  running/recoverable instance.

| Capability | Account plugin profile | Workspace preset |
|---|---|---|
| Plugin configuration | **Owns** | References |
| Channel configuration | **Owns** | References indirectly |
| Cooldown state | **Owns** (profile-scoped) | Tracks dependencies |
| Individual execution task | **Owns** | Orchestrates |
| Multi-account workflow state | — | **Owns** |
| Cross-account synchronization | — | **Owns** |

Implementation decisions (owner-locked):
1. Reference profiles, don't copy them.
2. Keep runtime state separate — profile cooldowns stay profile-scoped.
3. Workspace membership is explicit — presets reference only members.
4. Profile changes detected, not silently substituted.
5. Coordination is recoverable — workflow checkpoints enable resume.
6. No shared-state ownership ambiguity — workspace observes, never owns.

Phasing:

- **P1** — ownership model: profile identity, plugin-defined schemas,
  channel-in-profile, profile-scoped cooldowns/tasks, independent
  lifecycle, restart recovery
- **P2** — core persistence: generic profile storage API (CRUD +
  exists), JSON body (core never interprets), `UNIQUE(account, plugin,
  profile_name)`, schema versioning
- **P3** — plugin lifecycle: plugins define defaults + validation;
  profile CRUD/selection is plugin-managed (cog commands or hooks)
- **P4** — runtime execution: resolve profile before task start; pass
  profile config into execution; per-profile isolation
- **P5** — application interfaces: generic profile persistence through
  ControlAPI; plugin-provided management operations; CLI/GUI consume
  without imposing a universal schema
- **P6** — isolation testing: cross-account/plugin profile-name
  collisions; CRUD + restart persistence; concurrent execution;
  channel isolation; deletion/recovery
- **P7** — migration: existing `plugin_profiles` + bindings map into
  profile instances; extract `"channels"` key; merge `plugin_settings`
  overrides; drop redundant centralized preset mechanisms
- **P8** — workspace preset integration: reference `(account, plugin,
  profile)` tuples; workspace execution instances hold runtime state;
  profile-change detection; workflow checkpoints

Migration contract: `adoption_of_data_management.md` (the full
existing → new table/column mapping).

### Flet UI (pages deepen)

Preset/profile management, chain inspection, live WS events,
per-account plugin profile pages.

---

## Open Ideas (unscheduled)

[ ] Metric rollup wiring — the opt-in global view over
    AccountPluginData (store + local view shipped; aggregation missing).

---

## Codebase Hygiene

[x] Repository hardening, free tier (2026-11-15): security workflow
    (gitleaks full-history + pip-audit vs the lock + bandit -ll),
    pre-commit local gate, Dependabot (github-actions only), read-only
    workflow tokens, CODEOWNERS + SECURITY.md, bandit triage (8 sites,
    values always parameterized).
[x] Always-healthy workflows (2026-11-15): per-push gates — export-sync
    (the §8 contract, mechanically enforced) + mypy ratchet
    (.github/mypy-baseline = 485; grow = red, shrink locks in); weekly
    health sweep — full gate on a fresh runner (workflow_call reuse),
    packaging sanity (uv build + action-pin validation), dependency
    freshness probe (uv lock --upgrade → tracking issue).
[x] Build pipeline (2026-11-15): per-push build gate in tests.yml
    (sdist + wheel); build.yml — the v*-tag release pipeline (build from
    the exact tag, CHANGELOG notes attached). DELIBERATELY UNARMED: the
    tag ruleset carries no bypass, so no release can ship until the
    owner lifts the hold — structure ready, publishing deferred.
[ ] Gated on the public flip (owner-approved): repo rulesets (master:
    PR + required `unittest` check, no force push/delete; `v*` tags
    protected), GitHub native secret scanning + push protection,
    Dependabot alerts, then CodeQL (free once public).
[x] mypy strict ratchet: **COMPLETE (2026-11-15)** — 485 legacy errors
    fixed to ZERO across all 108 source files. Strict mode is now a
    hard floor (.github/mypy-baseline = 0). The pass surfaced and fixed
    four latent runtime bugs along the way (see the commit series).
[ ] `core/remote_control.py` — delete when the sim's control_mention
    scenario retires

---

## Removed / Superseded Systems

| System | Removed | Superseded by |
|---|---|---|
| ProfileManager (permission bundles) | 2026-10-22 | Direct `account_plugins` assignment |
| Mudae parser pack | 2026-11-13 | Generic parser registry (ships empty) |
| Dashboard (custom HTTP/WS/SPA) | 2026-11-13 | FastAPI + Flet |
| Sync groups + fleet | 2026-11-14 | Workspace unification (`core/workspace/`) |
| COMMANDS/CommandSpec/ArgSpec facade | 2026-11-15 | Cog declarations (the convergence) |
