# LibzentSuite — Roadmap

Planning and study documents live in `plans/` at the repo root — `docs/`
stays strictly user-facing functionality and command reference
(AGENTS.md §5). Binding contracts: `ARCHITECTURE.md`, `INTEGRATION.md`.

---

## Core Design Goal

    One Core API (ControlAPI)
          |
      +---+---+
      |       |
      CLI     Future UI (FastAPI + Flet)

Both interfaces manipulate the same: accounts + plugin assignments,
presets, tasks and scheduling, rate-limit policies + the safety floor,
credentials (encrypted at rest), logs and audit, simulation, workspace
orchestration. Plugins stay independent of the interface layer.

## Current State (2026-11-14)

**1,636 tests green, CI green.** Core framework (bootstrap →
ControlAPI → managers → repositories → adapters); multi-account Discord
orchestration on the better.py-self fork; workspace unification
complete (`core/workspace/`: membership, chains, durable signals,
presets, channels, guard, reconciler); plugin system with
EMITS/FOLLOWS; rate limiting (1.5 s floor, learned defaults);
Fernet-encrypted credentials; CLI (the only interface); full offline
simulator; uv-locked, ruff-gated, mypy-ratcheted.

Removed: dashboard (2026-11-13), Mudae parser pack (2026-11-13),
sync groups + fleet (2026-11-14 — replaced by the workspace layer).

---

## DONE — Workspace Unification (W0–W5, 2026-11-13 → 2026-11-14)

ONE concept replaced SyncGroupManager + the fleet module: accounts stay
independent until placed in a **workspace** owning membership, pacing
clocks, chains ("A acts → B follows, visibly"), presets (copy-on-apply
with provenance), and channels (registry / assignments / per-plugin
bindings).

- **W0** — spec: `WORKSPACE_DESIGN.md` (18 locked decisions, rev 3)
- **W1** — pure domain core (models, transitions, 7-check policy,
  presets) — 76 tests
- **W2** — persistence + the v10 fold (materialization, provenance,
  channels) — 20 tests
- **W3** — WorkspaceService (one writer) + the `workspaces_*`
  ControlAPI/CLI surface — 19 tests
- **W4** — chain runtime (durable signal outbox, immutable snapshots,
  visible skips, deterministic routing) + guard + reconciler — 23 tests
- **W5** — old modules deleted; v11 drops the v9 tables; the
  migration runner race-proofed at the root

Full decision record: `WORKSPACE_DESIGN.md`. Landing detail: the
commit series `0332ed4`..`cb70a63`.

---

## Active Queue

### NEXT — Command convergence (C1–C4; owner-locked 2026-11-14: cogs only · full convergence · before FastAPI)

**Plugins ARE cogs for their command face.** The parallel facade
retires: `COMMANDS`/`CommandSpec`/`ArgSpec`, manual tokenization
(`_parse_invocation`, `_convert_arg`), custom `render_help`, and the
922-line `SelfCommandHub` bridge. Plugins declare a real library Cog —
typed params via annotations (`target: discord.Member` resolves
mention/ID/name through the client cache), command groups, native
help, `cog_command_error`, `cog_before_invoke` audit. Framework verbs
become cogs in the same slice — ONE command model.

The seams (owner-refined):

- **Pacing** — `ctx.send` stays LIBRARY-NATIVE: command replies are
  invocation-gated (the access check bounds them). The documented
  discipline: `ctx.send` for COMMAND RESPONSES ONLY — never in loops,
  background work, or anything scheduled. Loops (and any future
  app-wide task runner) ride `ctx.services.send_message`, the paced
  pipeline.
- **Events** — two first-class paths, one toolbox: plugin cogs carry
  `@Cog.listener()` freely (the full library event surface; self-echo
  is standard `author.id` discipline). The event bus stays for the
  routed `handle_event` path. The DTO/audit layer is a FEATURE MODULE:
  `to_rich(message)` hands a listener the same frozen DiscordMessage
  the framework builds (embeds/components/buttons parsed once) and can
  fire the inbound audit — first-hand handling without per-plugin
  walkers. Injection rides the cog host (plugins never import
  adapters).
- **Cooldowns** — `commands.cooldown()` stays EXCLUDED (per-account,
  DB-persisted domain state).
- **Capability gating** — one cog instance per (account, plugin) with
  the services facade injected; assignment ∧ enabled become
  `add_cog`/`remove_cog` — the library's hot-reload IS the rebuild.
- **Framework intake** — the framework's own cog (audit + bus
  publication) is the single framework listener; plugin listeners run
  alongside, not behind.
- **Testing** — listeners are methods: direct invocation with
  constructed library fixtures (the fork is a pinned dependency; the
  standard cog pattern). The simulator is one harness, not the
  definition of testability — no feature is prohibited for being
  outside it.

Phasing: **C1 DONE 2026-11-14** (host + toolbox + before_invoke audit
+ intake cog; commits 415eab3..5f51cbc) → **C2 (in progress)** plugin
commands migrate
(fixtures first, then the declaration flip: COMMANDS removed, loader
validates cog classes) → **C3** framework verbs migrate; the hub
bridge + `COMMAND_HUB` registration retire → **C4** the hub keeps only
its hub-shaped residue (access wiring, verb definitions,
`api_provider`).

### FastAPI core API (after C1–C4)

FastAPI routers over the existing ControlAPI; Pydantic models; native
WebSocket for live events. Study: `plans/pivot-audit-fastapi-flet.md`.
Model the `workspaces_*` surface and the CONVERGED command model.

### Flet UI (after FastAPI)

Python-native UI; pages per module (accounts, workspaces, extensions,
control plane, system); plugin UI contribution contract redesign.

---

## Open Ideas (unscheduled)

### better.py-self adoption (remaining)

[ ] Bridge `on_http_throttle` → `RateLimitManager.report_server_limit()`
    — the real throttle signal (the fork absorbs 429s internally).
[ ] Per-route latency EWMA congestion watch (`http_route_latencies()`)
    — queuing inflation, not 429s, is how the send ceiling manifests.
[ ] Bounded probe-down recovery (decay ≤10% after 2 min clean, never
    below base) — the fork's lane-governor recipe.
[ ] `cache_stats()` through ControlAPI for the future System page.

DONE: dependency switch (`8637d0a`); memory caps + cache persistence
(`9f2321b`); `bot.add_check()` gate (`06cec37`); dynamic callable
prefixes — adminprefix lands live (`252aa78`).

### Plugin data

[ ] Metric rollup wiring — the opt-in global view over
    AccountPluginData (store + local view shipped; aggregation missing).

---

## Codebase Hygiene

[ ] mypy strict ratchet: 456 → 0 (event_bus, workspace, account_manager
    at zero; settings_manager + plugin_manager next)
[ ] `core/remote_control.py` — delete when the sim's control_mention
    scenario retires

---

## Removed / Superseded Systems

| System | Removed | Superseded by |
|---|---|---|
| ProfileManager (permission bundles) | 2026-10-22 | Direct `account_plugins` assignment |
| Mudae parser pack | 2026-11-13 | Generic parser registry (ships empty) |
| Dashboard (custom HTTP/WS/SPA) | 2026-11-13 | FastAPI + Flet (planned) |
| Sync groups + fleet | 2026-11-14 | Workspace unification (`core/workspace/`) |
