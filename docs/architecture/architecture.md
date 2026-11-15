# LibzentSuite — Application Architecture

**Revision:** 3.0
**Date:** 2026-11-14
**Status:** Binding engineering contract (verified against the current
codebase; the W0–W5 workspace unification has landed)
**Companions:** `INTEGRATION.md` (the detailed module contract),
`roadmap.md` (the active plan + decision records),
`WORKSPACE_DESIGN.md` (the unification spec — 18 locked decisions)

This document describes the architecture **as it exists in the
repository**. It is the product/UI-neutral engineering contract: what
owns what, which boundaries are binding, and the invariants the code
enforces. The next interface (FastAPI core API + Flet UI) wraps the
same facade — it changes no ownership described here.

---

## 1. Purpose

LibzentSuite is a multi-account Discord orchestration framework. One
process manages any number of user accounts, paces their outbound
actions to look like well-paced humans, coordinates work across them
(workspaces + chains), and exposes everything through the CLI
self-commands — one application facade underneath.

```
    One Core API (ControlAPI — 94 public methods)
          |
      +---+---+
      |       |
     CLI    Future interface (FastAPI + Flet)
```

### Product principles

1. **One orchestration concept** — the workspace owns membership,
   pacing clocks, chains, presets, and channels. (Sync groups and the
   fleet module were merged into it and deleted, 2026-11-14.)
2. Accounts are first-class resources with their own status, tasks,
   plugin state, and configuration — independent until placed in a
   workspace.
3. Configuration is layered with explicit precedence; presets apply by
   COPY with provenance, never live inheritance.
4. Operational state is observable and distinguishes configured intent
   from observed runtime state.
5. Safety systems mitigate risk; they authorize nothing. The pacing
   floor, server-limit handling, and audit trails are invariants, not
   features.

## 2. The composition model: Core Engine → Driver

The central architectural relationship is the owner's Core Engine →
Driver model:

```
┌──────────────────────────────────────────────────────────┐
│                   CORE ENGINE (framework)                │
│                                                          │
│  accounts · workspace orchestration · pacing · scheduler │
│  persistence · credentials · audit · event bus           │
│                                                          │
│  provides ABILITIES through the services middleware     │
└───────────────┬──────────────────────────────────────────┘
                │  PluginContext / PluginServices (authoritative)
                ▼
┌──────────────────────────────────────────────────────────┐
│                    DRIVERS (plugins)                     │
│                                                          │
│  decide WHEN an ability is used: react to events, run    │
│  commands, emit signals, follow chains                   │
└──────────────────────────────────────────────────────────┘
```

- The **Core Engine provides the ability** — every outbound action,
  state read, credential use, and signal emission is a framework
  service call that carries pacing, validation, audit, and security.
- The **plugin drives it live** — plugins hold no accounts, clients,
  database connections, or pacing state; they *utilize* what the
  engine provides, when their logic decides.
- **The services middleware is authoritative for security**: a plugin
  cannot bypass it because there is no other path to the platform.
  Plugins are NOT sandboxed Python — safety comes from the explicit
  surface, not isolation.
- Core stays game-agnostic: game-specific strings and recognition
  rules belong in parser packs and plugin code, never in `core/`.

## 3. System composition

`core/bootstrap.py::build()` is the ONE composition root. It wires
every manager, service, and adapter; modules communicate through
their contracts and injected dependencies — no service locators, no
hidden imports across boundaries, no global mutable wiring. The boot
sequence (verified order): credentials/config → database (migrations
apply under the write lock) → log service → event bus → limiter →
signal outbox → workspace service (+ pacing restore) → chain runtime
+ dispatch guard + reconciler → scheduler → plugin manager → account
manager → ControlAPI → framework handle. `Framework.shutdown()`
reverses it fault-tolerantly: every teardown step runs even if an
earlier one failed.

### Module map (current)

| Module | Owns |
|---|---|
| `core/api.py` | ControlAPI — the shared facade every interface calls |
| `core/workspace/` | THE orchestration: service (one writer), models, transitions, policy, presets, chains runtime, durable signals, dispatch guard, reconciler |
| `core/account_manager/` | account lifecycle, runtime client/cache, sender hook, lifecycle governor, waiter registry |
| `core/plugin_manager/` + `plugin_base.py` | the driver contract: discovery, per-account instances, dispatch, self-commands, EMITS/FOLLOWS validation, `invoke_handler` |
| `core/settings_manager.py` | the one settings schema every surface consumes |
| `core/rate_limit_manager/` | policies (global/account/plugin/pair), pacing, workspace clocks, 429s, warnings |
| `core/task_scheduler/` | queue, per-account lanes (concurrency 1), retries, cancellation, history |
| `core/event_bus.py` | typed in-process pub/sub (coalesced, bounded) |
| `core/access_manager.py` | the command gate: per-account allowlists, admin list (fail-closed) |
| `core/cooldowns.py`, `plugin_state.py`, `plugin_scopes.py` | plugin-facing persistence: cooldowns, scoped state, channel scopes |
| `core/credential_service.py`, `credentials.py` | Fernet-encrypted tokens; ids from `.env` |
| `core/audit.py`, `logging_service.py` | the audit trail and structured logs |
| `database/` | SQLite: ordered migrations (v11 current), repositories (workspaces, presets, signals, channels, accounts, plugins, …) |
| `adapters/discord/`, `adapters/simulator/` | the platform boundary: live client (better.py-self, lazy import) and the complete offline world |
| `interfaces/cli/` | REPL + one-shot commands — the only current interface |

## 4. The workspace orchestration layer

`core/workspace/` is the single owner of coordinated behavior
(WORKSPACE_DESIGN.md is the authoritative spec; this is the contract
summary):

- **Membership** — `accounts.workspace` is THE authoritative pointer;
  the `workspace_members` table is a same-transaction projection never
  independently writable. A move fences the old workspace (generation
  bump) and requeues stale tasks without touching account-owned
  configuration.
- **The one writer** — `WorkspaceService` persists first, then
  publishes; one command at a time under its lock. Repositories own
  rows, the service owns domain transactions.
- **Chains** — the run engine executes IMMUTABLE definition snapshots
  (edits affect future runs only); routing is deterministic
  (`specific_account` or `first_eligible` by seniority); every target
  passes the 7-check policy, and a denial is a **visible skip with the
  reason recorded** — never a silent drop, never a blind send.
- **Durable signals** — acceptance persists BEFORE acknowledgement;
  delivery is at-least-once with dead-lettering; consumers dedupe by
  `signal_id`. Signals authorize nothing.
- **Transitions** — `core/workspace/transitions.py` is the single
  validation source; nothing else decides a legal move. Generation
  fences reject stale dispatches; the reconciler repairs drift
  idempotently and never guesses.
- **The dispatch guard** — the scheduler's pre-dispatch gate. It
  decides WHETHER, the limiter decides WHEN; it can never weaken
  pacing. Non-member accounts are none of its business.
- **Presets** — copy-on-apply with provenance (`source_preset_id`,
  `source_version`, `applied_at`); a stale preview conflicts, never
  overwrites. Versions are immutable; updates are explicit.
- **Channels** — registry (identity + validity, only `available`
  resolves) / account assignments / per-plugin bindings initialized by
  copy then free to diverge. A channel-bound operation verifies
  usability at dispatch.
- **Cross-store deletion** — account removal is a durable, retryable
  workflow (`deletion_jobs`) spanning the main DB and the separate
  plugin-data store; failures quarantine with the error surfaced.

## 5. The driver contract (plugins)

A plugin is a `PluginBase` subclass discovered from a directory. It
declares: metadata, a `Setting` manifest, lifecycle hooks
(`on_load`/`on_enable`/`on_disable`/`on_unload`), event handling, and
optionally:

- **`COMMANDS`** — self-command verbs (`CommandSpec` + `ArgSpec`
  positional grammar) invocable from Discord with the account's
  prefix; handlers run with the same paced `services` facade.
- **`EMITS`** — bare signal names the plugin may emit via
  `await ctx.services.emit_signal(name, payload)` (namespaced
  `<plugin>:<name>` at consumption — no cross-plugin spoofing).
- **`FOLLOWS`** — triggers (a signal, or a terminal task event from an
  acting plugin) each naming one handler method; workspaces bind the
  declarations into named chains, and the runtime invokes handlers
  through `PluginManager.invoke_handler` with the per-account context.

The loader validates declarations at discovery and fails a broken one
at boot (fail-fast, the `COMMANDS` convention). Capability on an
account is the direct `account_plugins` assignment — fail-closed: no
assignment, no plugins. Do not reintroduce permission bundles.

## 6. Safety and data semantics (binding)

- **Pacing floor**: no configured interval below
  `MIN_ACTION_INTERVAL = 1.5` s (cap `MAX_INTERVAL = 86400` s). Server
  limits (429/Retry-After) are always respected and earn paced
  retries. The workspace policy may allow, block, or defer — never
  weaken pacing.
- **The one pipeline**: `PluginServices` → `TaskScheduler.submit` →
  one in-flight action per account → `RateLimitManager.acquire` →
  final validation → sender hook → adapter → audit/history/events.
  Retries only for scheduler-classified transient errors; timeouts
  are terminal (an in-flight send cannot be un-sent).
- **Snowflakes are strings end-to-end** — Python, JSON, persistence,
  logs, any future browser state. Never `Number` in JavaScript.
- **Credentials**: write-only surfaces, Fernet-encrypted at rest, the
  master key outside the database. Tokens never render into logs,
  responses, or exceptions.
- **Scopes compose most-restrictive-wins**: per-task
  `(account, action)` over per-workspace `(workspace:<name>, action)`;
  learned per-action defaults apply when no policy is stored.

## 7. Events and persistence

- Typed events communicate observations; **an event is never
  authorization** for an operation. Durable changes go through the
  owner service, validated, persisted, then published.
- The adapter converts raw library objects into immutable DTOs ONCE at
  the boundary; runtime-only `raw` handles never persist.
- Repositories own persistence; services own transactions. Applied
  migrations are append-only (the runner serializes each migration
  under the write lock — parallel first boots cannot interleave).
  `Database(":memory:")` stays usable by tests/simulation.
- Plugin documents (`state/plugin-data.db`) keep the fixed lattice
  account → plugin → guild → channel: standalone copies, optimistic
  version writes, conflicts surfaced — never silently merged.

## 8. Lifecycle and testing

- `build()` boots the whole stack; shutdown reverses it in order
  (scheduler drain → periodic tasks → persistence queues → plugins →
  plugin-data store → database), every step fault-tolerant.
- Tests are deterministic: injected clocks, in-memory databases, the
  simulator adapter. No real credentials or network in routine tests.
  Focused suites first, then adjacent, then the full suite (~1,636
  tests). Never weaken a test to reach green.
- The project is built and its suite runs green on Linux; other
  platforms are untested.

## 9. Recorded architecture decisions

The binding decision record is `roadmap.md` (18 locked workspace
decisions + their provenance) and `WORKSPACE_DESIGN.md` (the full
spec). Historical inventories live under `plans/` (superseded material
is banner-marked, never silently rewritten).
