# LibzentSuite — Roadmap

**Renamed 2026-11-13** from TODO.md → roadmap.md (the file's history
follows the rename). Planning and study documents live in `plans/`
at the repo root — `docs/` stays strictly user-facing functionality
and command reference (AGENTS.md §5).

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
- Workspace orchestration (replacing fleet; the plan below)

Plugins remain independent from the interface layer.

---

## Current State (2026-11-13)

**What exists and is tested (1,673 tests green, CI green):**
- Core framework: bootstrap → ControlAPI → managers → repositories → adapters
- Multi-account Discord orchestration (better.py-self fork, lazily imported)
- Plugin system: discovery, lifecycle hooks, settings, self-commands, per-account instances
- Plugin profiles: named settings presets, account-to-preset bindings
- AccountPluginData: per-channel operational data (own SQLite store, optimistic versioning)
- Sync groups: persistent membership + shared pacing clock — **deprecated, superseded by Workspaces (W5 removes it)**
- Fleet orchestration: task lifecycle, handoff, cooldowns, reconciler, dispatch guard — **deprecated, superseded by Workspaces (W5 removes it)**
- Rate limiting: 1.5 s safety floor, learned defaults, server-cooldown handling, send lane
- Credential service: Fernet-encrypted at rest, key outside DB, write-only in UI
- Parser registry: generic, empty, game-packs plug in via `register_parser()`
- CLI: REPL + one-shot commands (the only interface currently)
- Simulator: complete offline Discord replacement with scenarios
- Build: uv-locked, ruff-gated, mypy-ratcheted (456 baseline and falling), CI on GitHub Actions

**What was removed (2026-11-13):**
- The entire dashboard interface (custom HTTP server, WebSocket, SPA, builder contract)
- The Mudae parser pack (~3,000 lines of game-specific recognition)
- All Mudae references (renamed to "primary bot" — game-agnostic)

---

# THE PLAN — Workspace Unification

Replaces SyncGroupManager + the fleet module with ONE concept:
accounts stay independent (per-account plugin assignment, unchanged)
until someone adds them to a **Workspace**, which owns membership,
orchestration state, and coordinated chains — "account A does smth,
follow-up account B cleans it up, if settings/plugins allow it."

```
Workspace → Multiple Accounts → Plugin assignment → Orchestration
```

## Locked decision record (owner, 2026-11-13)

| # | Decision | Choice |
|---|---|---|
| 1 | Follow-up trigger | **Hybrid** — task-terminal events (mechanical default, already on the EventBus) + plugin-emitted signals (semantic) |
| 2 | Chain definition | **Both layers** — plugins declare what they EMIT / FOLLOW; workspaces bind the declarations into named chains |
| 3 | Inter-step pacing | **Shared clock only** — workspace membership keeps full serialization (max anti-flag safety; today's sync-group semantics) |
| 4 | Membership cardinality | **At-most-one** workspace per account (single pointer column; existing invariants preserved; loosen later only on real need) |
| 5 | Migration | **Big-bang** — one schema version folds groups+fleet into workspaces; old tables drop in the same version |
| 6 | Fleet survivors | **Core survives, chains replace relay** — transitions tables, generation fences, reconciler, policy check order carry over renamed; observations/relay + FleetTaskHandler are redesigned as the chain layer |
| 7 | API surface | **Clean replace** — `workspaces_*` replaces `groups_*` + `fleet_*`; no compat aliases (surface debt is lowest now) |
| 8 | Workspace config layer | **REVISED 2026-11-14** — copy-only workspace layer: channel default suggestions + preset templates, applied by explicit copy onto accounts. Live plugin settings are strictly account-bound — no workspace/global settings inheritance (replaces the original live workspace > account > plugin precedence) |
| 9 | Preset semantics | **Copy-on-apply + provenance** (2026-11-14) — apply copies values into the account's own config with source_preset_id/source_version/applied_at; updates explicit; detach preserves values. Today's live-bound profiles end at v10 |
| 10 | Preset portability | **Plugin-bound by default** (2026-11-14) — cross-plugin reuse only via explicit export/import re-validated against the destination plugin's schema |
| 11 | Membership source of truth | **`accounts.workspace` is the sole authoritative pointer** (2026-11-14 rev 3) — workspace_members is a same-transaction derived projection, never independently writable, reconciler-checked |
| 12 | Signal delivery | **Durable before ack, at-least-once** (rev 3) — signal/outbox row persisted first; consumers deduplicate by signal_id and are idempotent |
| 13 | Chain target selection | **Explicit per-step routing** (rev 3) — `specific_account` + deterministic `first_eligible` (joined_at ASC, name ASC tie-break, target persisted before execution); fan-out deferred |
| 14 | Chain definition changes | **Immutable run snapshots** (rev 3) — each run executes a validated definition snapshot captured at creation; edits affect future runs only |
| 15 | Preset versions | **Immutable versions** (rev 3) — updating creates a new version; applied configs keep source_preset_id + source_version |
| 16 | Preset apply concurrency | **Atomic + revision-checked** (rev 3) — stale previews conflict, never overwrite intervening edits |
| 17 | Clock vs execution | **Four separate contracts** (rev 3) — rate coordination / task concurrency / chain ordering / mutation serialization; a shared clock is not an execution mutex |
| 18 | Account deletion | **Durable retryable cross-store workflow** (rev 3) — deletion_jobs with observable completion, bounded retries, residual data quarantined (flagged + warned, never silently abandoned) |

## Entity mapping (what folds where)

```
TODAY (two modules, seam-wired)          WORKSPACE (one owner)
─────────────────────────────────        ─────────────────────────────
sync_groups (identity, enabled)    →     workspaces (identity, enabled,
fleet_groups (generation, paused)  →               generation, paused)
accounts.sync_group (pointer)      →     accounts.workspace (pointer)
fleet_members (mode, eligibility)  →     workspace_members (membership +
                                         mode + eligibility as columns)
fleet_assignments (work units)     →     workspace_tasks (+ chain steps)
fleet_cooldowns (policy records)   →     workspace-scoped cooldowns
fleet_transitions (history)        →     workspace transition history
bootstrap fleet/group closures     →     (deleted — membership written
                                         where it's owned)
```

## Phases

### W0 — Spec + contracts *(root docs only)*
[x] `WORKSPACE_DESIGN.md` (repo root) — DONE 2026-11-14: entity model +
    ERD, the decision record, trigger catalog (with the
    TaskCompleted/TaskFailed `plugin`-field gap named for W4), chain
    + step-state model (SKIPPED is visible, never silent), settings
    precedence chain, v10 migration mapping, `workspaces_*` surface,
    carry-over vs new-surface table, W1-resolved items
[x] Knowledgebase append-only addendum — DONE 2026-11-13:
    `knowledgebase/WORKSPACE_SUPERSESSION.md` (local, gitignored area)
[ ] Owner review and approval of the spec before any code moves —
    **the remaining W0 gate: WORKSPACE_DESIGN.md awaits review**

### W1 — Domain core *(pure, no I/O)*
[ ] `core/workspace/models.py` — WorkspaceRecord, WorkspaceMember
    (mode + eligibility columns), WorkspaceTask/step states,
    ChainDefinition, trigger/signal specs (adapted from
    `core/fleet/models.py`, which is already a PURE module)
[ ] Channel + preset domain models: ChannelAssignment/
    ChannelBinding, Preset (3 scopes) + provenance record
    (source_preset_id, source_version, applied_at), and the pure
    preset lifecycle functions (validate/apply/update-preview/
    detach semantics — WORKSPACE_DESIGN.md §6)
[ ] `core/workspace/transitions.py` — carry fleet's tables over +
    chain-step states (`PENDING → READY → RUNNING → DONE | FAILED | SKIPPED`)
[ ] Plugin capability contract replacing `fleet_handler`:
    declarative `EMITS` / `FOLLOWS` on PluginBase
[ ] `core/workspace/policy.py` — fleet's check order extended with
    the new gate: *plugin assigned ∧ enabled on the target account ∧
    settings gate passes ∧ a channel binding resolves* (WORKSPACE_DESIGN
    §12 — the WHERE is part of eligibility)
[ ] Pure unit tests green with zero connections (rollout-A discipline)

### W2 — Persistence + big-bang migration
[ ] Schema v10 per WORKSPACE_DESIGN §13: `workspaces`,
    `workspace_members`, `workspace_tasks`, `workspace_chains`,
    `workspace_transitions`, `presets` (3 scopes + version),
    `account_channels`, `account_channel_assignments`,
    `account_plugin_channels`; `accounts.sync_group` →
    `accounts.workspace`; fold + drop the fleet/sync tables
[ ] Live-binding materialization: every `account_plugins.profile`
    binding copies its effective keys into `plugin_settings` with
    provenance; the profile column drops; the reserved `"channels"`
    key extracts into `account_plugin_channels`
[ ] `PluginDataStore.drop_account(account)` — the cross-store
    deletion boundary for account removal (fixes the inheritance
    gap found 2026-11-13)
[ ] Typed repositories following the Protocol patterns (see
    `core/sync_group_manager/manager.py` + `core/fleet/service.py`
    for the established shape)
[ ] Migration test proves a populated v9 DB lands correctly in the
    workspace shape — memberships, generations, cooldowns,
    post-materialization effective settings, channel bindings

### W3 — WorkspaceService (the one writer)
[ ] `core/workspace/service.py`: membership + orchestration under the
    single-lock persist-first discipline (FleetService's proven
    shape, minus the seam closures)
[ ] Channel + preset operations: account channel add/remove/set,
    plugin bindings (initialize-by-copy from account assignments),
    preset create/validate/apply/update(explicit diff)/detach/
    export/import — every apply is a copy with provenance
    (architecture rules 1-6, WORKSPACE_DESIGN §5-6)
[ ] Settings resolution: the simplified chain (defaults ← global ←
    account — the profile layer ends, decision 9) with the
    precedence-collision test matrix
[ ] Account-removal cascade: the full deletion boundary across BOTH
    stores (WORKSPACE_DESIGN §7 — plugin-data documents included)
[ ] Pacing: membership drives the limiter mapping
    (`workspace:<name>` shared clock — engine key rename rides the
    clean replace)
[ ] ControlAPI surface per WORKSPACE_DESIGN §14 (workspaces_* +
    channels + presets; groups_*/fleet_* removed) + CLI verbs
[ ] Service transition/race tests green (the A→X racing A→Y
    lock discipline)

### W4 — Orchestrator (the chain engine — the headline feature)
[ ] Trigger intake: subscribes to task-terminal bus events +
    new `services.emit_signal(...)` plugin API
[ ] Chain runtime: trigger → resolve next step → eligibility gate
    (W1 policy) → dispatch to a member account
[ ] Workspace-level chain bindings over plugin declarations
[ ] Reconciler carried over: 30 s idempotent passes over chain runs
    (zombie fencing via generation bumps — unchanged)
[ ] Simulator scenarios prove: A acts → B follows; B's plugin not
    assigned → step SKIPPED (visible, not silent); settings gate
    denies → SKIPPED with reason; failure/retry semantics

### W5 — Retire the old modules
[ ] Delete `core/sync_group_manager/` and `core/fleet/`, their
    repositories, remaining references
[ ] Docs sweep: user-facing `docs/` updated to the workspace
    surface; knowledgebase addenda; this roadmap
[ ] Full suite green with both old modules gone

**Carry-over guarantee:** transitions tables, generation fences,
reconciler passes, and the policy check order move
renamed-not-redesigned — the correctness machinery stays exactly as
tested. **New surface:** chain runtime, signal API, workspace
settings layer, the plugin-eligibility gate.

---

## Active Queue

[ ] **Workspace W0 review** — `WORKSPACE_DESIGN.md` is written and
    awaiting owner approval (the last W0 item); W1 starts on approval

[ ] **FastAPI core API** — replace the deleted dashboard's REST surface
    with FastAPI routers calling the existing ControlAPI. Pydantic
    request/response models. Native WebSocket for live events.
    Study: `plans/pivot-audit-fastapi-flet.md` (phase 2).
    NOTE: model the workspace surface (`workspaces_*`), not
    `groups_*`/`fleet_*` — build against the target, not the
    deprecated layer.

[ ] **Flet UI** — replace the deleted SPA with a Python-native UI.
    Pages per module (accounts, workspaces, extensions, control
    plane, system). Plugin UI contribution contract redesign.
    Study: `plans/pivot-audit-fastapi-flet.md` (phase 3).
    Same note: page model targets workspaces.

[ ] **PHASE C LIVE DEBUG** — bravo re-credentialed and running; alpha
    still needs a fresh credential. Remaining: admin-command flow
    B -> A, ordered full walkthrough. (Can run against the current
    fleet layer; re-verify the admin verbs after W3 swaps the
    surface.)

---

## Open Ideas (unscheduled)

### ~~Fleet enhancement~~ — SUPERSEDED by the Workspace plan (2026-11-13)

The items below described the fleet layer that W5 removes. Kept
visible for traceability; do not build against them.

~~[ ] Metric rollup wiring~~ — the AccountPluginData half stays
    valid (see "Plugin data" below); the FLEET dashboard view half
    is superseded by the workspace surface.
~~[ ] cooldown_key policy mapping (TaskDefinition → FleetPolicy)~~ —
    superseded by the W1/W4 chain policy gate.
~~[ ] FleetTaskHandler first real consumer~~ — superseded: chains
    replace the capability (decision #6).

### Plugin data

[ ] Metric rollup wiring: declared metric hints (sum|last) surfaced
    through the future FastAPI dashboard — the opt-in GLOBAL view over
    AccountPluginData (the store + LOCAL view shipped; only the
    aggregation surface is missing)

### Rate limiting (better.py-self integration)

[ ] Bridge `on_http_throttle` to `RateLimitManager.report_server_limit()`
    — the REAL throttle signal for user accounts (library absorbs 429s
    internally; we currently only see the ones it doesn't). Wire: hook the
    event in `adapters/discord/client.py` → publish `RateLimitTriggered`
    on the EventBus → limiter calls `report_server_limit()`.
[ ] Add per-route latency EWMA congestion watch (`http_route_latencies()`)
    — queuing inflation, not 429s, is how the send ceiling manifests.
    Add an EWMA monitor to `core/rate_limit_manager/engine.py` that
    reads `client.http_route_latencies()` and warns when the send route
    exceeds a threshold.
[ ] Add bounded probe-down recovery (decay backoff ≤10% after 2 min clean,
    never below the base interval) — matches the fork's lane governor recipe
    (`examples/lane_governor.py`). Apply to `RateLimitManager`'s
    cooldown recovery path.
[ ] Expose `cache_stats()` through ControlAPI — add a `system_cache_stats()`
    method that aggregates per-account `client.cache_stats()` snapshots
    for the future dashboard's System page.
[x] Add memory caps + cache persistence to the Client constructor in
    `adapters/discord/client.py` — DONE 2026-11-13 (commit 9f2321b):
    `_fork_memory_options()` now feeds both the Bot and plain-Client
    paths: `max_member_cache=500, max_total_member_cache=5000,
    max_thread_cache=200, max_total_thread_cache=1000,
    gc_freeze=True, dispatch_backlog_warning=500,
    persistence_path=state/<account>-cache.bin, persistence_max_age=86400`.
    Persistence is keyed to the account name (omitted when nameless);
    write/replay failures never break a connect.
[x] Switch to better.py-self — DONE 2026-11-13 (commit 8637d0a):
    dependency switched, drop-in verified, 1,671 tests green. The
    remaining fork-adoption items are the [ ] entries in this section.

### Command system (ext.commands deep adoption)

[x] Move the authorization gate to `bot.add_check()` in
    `adapters/discord/client.py` — DONE 2026-11-13 (commit 06cec37):
    the gate is now `_bot_invoker_gate`, installed once per client in
    `_install_command_layer` via `bot.add_check()`; the library evaluates
    it in `Command.prepare` → `can_run` BEFORE argument parsing.
    Unauthorized invocations raise `CheckFailure`, which
    `on_command_error` drops silently alongside `CommandNotFound`.
    AccessManager stays the domain owner; reconnects re-install per
    fresh client (no double registration).
    NOTE: do NOT adopt `commands.cooldown()` — our cooldowns are
    per-account, DB-persisted, fleet-aware domain state.

[ ] Replace `_parse_invocation()` with library-native argument parsing.
    Current: `adapters/discord/client.py` manually tokenizes
    `"sm!rollstart 123"` into `(["123"], "123")` using regex + manual
    prefix stripping. The library's parameter parsing already does this
    via type annotations on the command callback:
    ```python
    async def _invoke(ctx, target: discord.User, count: int = 1):
        # ctx.args/ctx.kwargs already parsed and typed
    ```
    This eliminates `_parse_invocation` (~60 lines) and the manual
    `ArgSpec` type-coercion path in `core/plugin_base.py::_convert_arg`.

[ ] Adopt library converters in `ArgSpec` — add a `converter` field
    (`discord.Member | discord.User | discord.TextChannel | str | int`)
    that maps to the library's converter system. When a plugin declares
    `ArgSpec("target", discord.User, ...)`, the library resolves the
    argument from ID/mention/name automatically. Available converters:
    `MemberConverter, UserConverter, TextChannelConverter, RoleConverter,
    EmojiConverter, GuildConverter, MessageConverter` + `Greedy[T]`,
    `Range[int, min, max]`, `clean_content`.

[ ] Simplify `_invoke` pass-through — with `bot.add_check()` handling
    authorization and library parsing handling arguments, the thin
    command wrapper in `_register_self_command()` becomes a straight
    dispatch + paced reply (no auth check, no manual parsing).

[ ] Framework verbs as Cogs — `core/plugin_manager/commands.py`
    (922 lines) manually bridges `CommandSpec` → `bot.command(name=...)`
    via `_register_self_command()` + `_rebuild_self_commands()` diff
    tracking. Cogs replace this (add_cog/remove_cog hot-reload,
    cog_command_error, built-in help). Plugin `COMMANDS` declarations
    still go through the hub for per-account assignment/enable gating —
    cogs are for the framework verbs only.

[ ] `before_invoke` for audit logging — runs after checks pass, before
    the command callback: clean separation of authorization (check),
    audit (hook), and execution (callback).

[ ] Leverage `Context` methods — bridge `ctx.send` through our paced
    pipeline (scheduler → limiter → adapter), preserving the safety
    floor while using the library's API surface.

---

## Codebase Hygiene

[ ] mypy strict ratchet: 456 errors → 0 (2026-11-13: event_bus +
    sync_group_manager + fleet + account_manager already at 0 direct
    errors — whole-tree 559 → 456. Continue module by module;
    settings_manager and plugin_manager are the next-largest)
[ ] `core/remote_control.py` (365 lines): only the simulator's
    `control_mention` scenario uses it — delete when that scenario
    is retired

---

## Removed / Superseded Systems (context for new contributors)

| System | Removed | Superseded by |
|---|---|---|
| ProfileManager (permission bundles) | 2026-10-22 | Direct account→plugin assignment (`account_plugins` table) |
| Mudae parser pack | 2026-11-13 | Generic parser registry (`parsers/manager.py`, ships empty) |
| Dashboard (custom HTTP/WS/SPA) | 2026-11-13 | FastAPI + Flet (planned) |
| Sync groups + fleet (in code, deprecated) | planned (W5) | Workspace unification (this roadmap) |
