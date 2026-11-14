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
- Workspace unification: `core/workspace/` (service, chains, signals, guard, reconciler)
- Plugin system: discovery, lifecycle hooks, settings, self-commands, per-account instances
- Plugin profiles: named settings presets, account-to-preset bindings
- AccountPluginData: per-channel operational data (own SQLite store, optimistic versioning)
- Workspace orchestration: membership, chains (A acts → B follows), presets, channels, durable signals — **the unified layer (replaced sync groups + fleet 2026-11-14)**
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
[x] Owner review and approval of the spec before any code moves —
    DONE 2026-11-14: the owner's go ("proceed with the roadmap and
    start the implementation process") opened W1

### W1 — Domain core *(pure, no I/O)* — [x] DONE 2026-11-14 (see W1 commits)
[x] `core/workspace/models.py` — DONE: eligibility/task vocabulary
    carried renamed (fleet tables map one-to-one — equivalence is
    TESTED), ExecutionMode group_managed→managed, plus the new
    surface: StepStatus (+CANCELLED), ChainRunStatus (PARTIAL
    first-class), ChannelValidity (5 states, only AVAILABLE usable),
    ChannelDescriptor/ChannelBinding, RoutingPolicy, FollowSpec +
    validate_plugin_declarations, SignalIdentity (plugin-namespaced
    at consumption), ChainStep/ChainDefinition/ChainRun/
    StepExecution, Provenance, PresetScope, WorkspaceEvent envelope.
    mypy strict clean.
[x] Channel + preset domain models — DONE: descriptors/bindings
    above + `core/workspace/presets.py` (PresetRecord with
    scope-owner consistency + owner-scoped uniqueness key, the
    SettingSpec Protocol keeping the module pure, validate/apply
    (preset's keys only — the locked D.3 semantics)/StalePreview
    (decision 16)/detach)
[x] `core/workspace/transitions.py` — DONE: member + task tables
    carried VERBATIM (fleet equivalence asserted by test), new
    STEP_TRANSITIONS (visible-skip-with-reason enforced at the
    validator level, RUNNING never silently skips, retry-gated
    FAILED→READY) + CHAIN_RUN_TRANSITIONS (PARTIAL terminal)
[x] Plugin capability contract — DONE: `EMITS`/`FOLLOWS` ClassVars
    on PluginBase (chain-inert defaults), `fleet_handler` marked
    deprecated (W5 deletes), loader validates declarations at
    discovery with the COMMANDS fail-fast convention (missing
    handler = load error; namespaced EMIT = load error)
[x] `core/workspace/policy.py` — DONE: the 7-check order as a PURE
    WorkspacePolicy.evaluate(DispatchFacts) — facts bundled by the
    caller, zero seams, check 7 (plugin ∧ gate ∧ usable channel)
    produces the "visible skip" reasons; membership boundary and
    framework-safety-first carried
[x] Pure unit tests green with zero connections — DONE: 76 tests
    across 5 modules (models/transitions/policy/presets/
    declarations incl. loader discovery); full suite 1,749 green;
    whole-tree mypy 456 with ZERO new errors

### W2 — Persistence + big-bang migration — [x] DONE 2026-11-14 (W2 commits)
[x] Schema v10 — DONE: the additive fold (WORKSPACE_DESIGN §13; the
    physical DROP of v9 tables is v11, landing when W3 unwires the
    readers, so every intermediate commit boots green): workspaces
    (identity + folded generation/paused), accounts.workspace pointer
    (backfilled), workspace_members projection (fleet mode/eligibility
    LEFT-JOINed, group_managed→managed), presets (row PK +
    UNIQUE(preset_id, version) — identity ≠ row id so immutable
    versions fit; plugin_profiles folded with ids preserved for
    provenance), provenance columns on account_plugins, the
    live-binding materialization (SQLite boolean-typing handled via
    json_each's type column — the §13.2 effective-values invariant
    is TESTED), channels extraction into account_plugin_channels
    (both config sources), workspace_tasks/cooldowns/transitions
    folds (scope renames), chains/runs/step-executions/signals/
    deletion_jobs tables (W4-ready)
[x] Typed repositories — DONE: WorkspaceRepository (state/member
    projection/tasks/cooldowns/history/deletion jobs; members
    version-bumped, projection-write-only), PresetsRepository
    (identity-scoped create, IMMUTABLE publish_version, version()
    provenance reads, archive), AccountChannelsRepository (registry
    observe-never-touches-selections, add-one/replace-set verb
    split, dependency-refused deletes with named conflicts,
    drop_account boundary). Wired on Database alongside their v9
    predecessors
[x] `PluginDataStore.drop_account` — DONE: idempotent, per-account,
    refuses empty names; the cross-store deletion boundary
[x] Migration test — DONE: populated v9 fixture (every fold source,
    overlapping overrides, shared profiles, channels in both layers)
    → v10 with the effective-values invariant asserted; fresh-DB and
    idempotence paths covered (9 tests + 11 repo tests)

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

[ ] **Workspace W3 (remainder)** — DONE so far: the service (one
    writer), the workspaces_* ControlAPI surface + CLI verbs, the
    bootstrap/simulator rewiring (fleet assembly retired from boot).
    DONE 2026-11-14: W4 (signals outbox, ChainRuntime, guard +
    reconciler, full boot/sim wiring — A-acts-B-follows proven by the
    runtime suite) and W5 (core/fleet + core/sync_group_manager
    deleted with their repositories; migration v11 drops the v9
    tables + accounts.sync_group; the migration runner is race-proof
    at the root). THE WORKSPACE UNIFICATION IS COMPLETE.
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
| Sync groups + fleet | 2026-11-14 (W5) | Workspace unification (`core/workspace/`, this roadmap) |
