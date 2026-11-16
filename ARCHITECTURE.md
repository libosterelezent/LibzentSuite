# LibzentSuite — Architecture

## Purpose and status

LibzentSuite is a game-agnostic, multi-account Discord flow-orchestration framework. Its product shape is a web dashboard paired with a long-running automation framework. Mudae is the first workload, not the organizing concept of the core.

This document records the current architectural boundaries and the invariants future changes must preserve. It is not a frozen screen specification: the UI, component library, routes, and public application API may evolve when a task requires it.

The repository's implementation is the evidence for what currently exists. The current product requirements and an explicitly approved task determine the intended change. Tests are executable evidence of behavior, but an old test must be revised when its assertion conflicts with an approved requirement. When source, tests, and requirements disagree, identify the conflict and resolve it explicitly rather than silently treating the existing implementation as the desired design.

For the detailed boundary between modules, see `INTEGRATION.md`. `TODO + Ideas.md` records project status and outstanding work; completed work must not be described as future work, and unscheduled ideas must not be promoted into the active queue without authorization.

## System overview

```text
                    CLI                  Dashboard / Web UI
                     |                         |
                     +------------+------------+
                                  |
                                  v
                         core.api.ControlAPI
                                  |
                  +---------------+----------------+
                  |               |                |
             AccountManager   PluginManager   SyncGroupManager
                  |               |                |
                  +---------------+----------------+
                                  |
                           TaskScheduler
                                  |
                          RateLimitManager
                                  |
                         AccountManager sender
                                  |
                    adapters/discord or simulator
                                  |
                         Discord API / SimWorld

Cross-cutting: EventBus, structured logging, audit, credentials,
settings, repositories, and the composition root in core/bootstrap.py.
```

The diagram is conceptual, not a promise that every manager directly calls every other manager. Dependency wiring belongs in `core/bootstrap.py`; modules communicate through their defined contracts and injected dependencies.

## Non-negotiable invariants

### One application boundary for interfaces

- CLI and dashboard mutations go through the same `core.api.ControlAPI` facade.
- The facade may be extended when a legitimate feature needs a new operation. Extend its implementation and contract, route the operation through it, and update tests. Do not create a parallel dashboard-only or CLI-only business-logic path to avoid changing the facade.
- Transport layers parse and validate their inputs, map them to the application contract, and translate results/errors to their own response format. They do not duplicate domain rules.

### Plugins are independent of the interface layer

- Plugins do not own accounts, global pacing, the database connection, dashboard state, or CLI state.
- Plugins request supported capabilities through `PluginContext` / `PluginServices` (`ctx.services`).
- The framework owns lifecycle, persistence, execution scheduling, pacing, audit, and cross-account coordination.
- Core code uses generic domain language. Game-specific messages, command strings, locales, and recognition rules belong in parser packs and workload/plugin code, not in `core/`.

### Safety and state belong to the backend

- Outbound Discord actions use the task scheduler, rate-limit manager, and adapter path. A UI control must not bypass that pipeline.
- The rate limiter owns action timing and enforces its configured safety floor. Fleet/group policy determines eligibility and ownership; it must not replace or weaken pacing.
- Server-provided rate limits are always respected.
- Persisted state is validated at boundaries. Bad input produces a useful domain/API error, not an unhandled process crash.
- Secrets are never rendered back into the UI or written to logs. Account tokens are stored encrypted; `.env` contains IDs/auth switches and other appropriate non-token configuration.

## Account and plugin model

### First-class account

Persistent account information is represented by database records. Runtime state—connection status, client, cache snapshot, latency, uptime, supervisor, and error information—belongs to the account runtime. Views must distinguish persisted configuration from runtime observations and must not invent missing state.

Dashboard account details should use the existing client cache snapshot for Discord runtime information. Do not add Discord API fetches simply to make a dashboard card look more complete. If new data is truly needed, document the requirement and route it through a supported, on-demand service.

### Direct plugin assignment and settings presets

The current capability model is direct `Account → Plugin` assignment, persisted in `account_plugins`. Assignment means the plugin is attached to that account; it is separate from the per-account enabled/disabled state. An account without assignments runs no plugins (fail-closed).

`plugin_profiles` holds named per-plugin settings presets and account-to-preset bindings. It is not the removed legacy multi-plugin `ProfileManager` system. The removed `profiles`, `profile_plugins`, and `account_profiles` design must not be reintroduced as a workaround.

### AccountPluginData

Operational plugin data is distinct from settings presets. It is persisted through the separate `state/plugin-data.db` data flow, with the fixed scope lattice `account → plugin → guild → channel`, standalone documents, optimistic version writes, and access through supported `ctx.services.data_*` methods. The plugin owns the payload; the framework owns scope, storage boundaries, and conflict semantics. Do not add inheritance between channel documents unless a future approved design changes this invariant.

## Group membership, fleet policy, and execution

### Sync groups

`SyncGroupManager` owns persistent sync-group records and account membership. An account belongs to at most one sync group. The manager informs the rate limiter of the current group mapping but does not absorb rate-limit ownership. Removing a group must not silently remove its accounts; non-empty groups must be emptied or explicitly detached first.

### Fleet orchestration

Fleet orchestration lives under `core/fleet/` and is separate from persistent sync-group membership and pacing.

- `FleetService` is the authoritative writer for fleet operations and persisted state transitions.
- `core/fleet/transitions.py` owns transition validation tables.
- `FleetPolicy` determines whether work is currently eligible to run.
- The scheduler enforces dispatch checks and the rate limiter decides when an approved outbound action may proceed.
- Observations (including parser findings and connection changes) authorize nothing by themselves. They may result in a restriction only through validated service operations.
- Generation/version fences prevent stale assignments or dispatches from running after a relevant state change.
- The reconciler repairs safe inconsistencies after boot, reconnects, or periodic passes. Ambiguous state must be surfaced for review rather than guessed away.
- Task identity and history survive handoff. A failed destination validation must not destroy the source assignment.

A suspended task returns to the queue when the service's resume/requeue operation permits it; labels in the UI should describe the actual transition, not imply a paused execution can continue in place if it is instead re-queued.

## Outbound action pipeline

```text
PluginServices request
        -> TaskScheduler submit
        -> per-account execution lane (one in-flight action per account)
        -> RateLimitManager.acquire (server cooldowns, pacing, jitter)
        -> final service/cache/policy checks
        -> AccountManager sender hook
        -> Discord adapter or simulator
        -> audit/history/event publication
```

- All outbound actions that the framework supports must follow this pipeline.
- Per-account concurrency remains one in-flight action; cross-account scheduling may provide fairness.
- Retry only errors classified as transient by the scheduler. A policy/validation block should fail with its human-readable reason rather than be retried blindly.
- Server 429 information is passed to the rate limiter and respected before retrying.
- Audit/history bookkeeping must happen consistently for terminal outcomes; avoid duplicated terminal records.

The rate-limit floor is a safety invariant: no configured action interval may be set below `MIN_ACTION_INTERVAL = 1.5` seconds, and the configured cap is `MAX_INTERVAL = 86400.0` seconds. Any change to that policy requires an explicit product/safety decision and corresponding tests.

## Events, cache, and persistence

- `core/event_bus.py` provides typed in-process events. Events communicate observations/state changes; they are not a substitute for validating a requested operation.
- Discord library objects are converted at the adapter boundary to immutable framework DTOs. Runtime-only raw objects are not persisted or represented in logs.
- Missing optional event data stays absent (`None`) unless the operation explicitly requests a supported lookup. Do not trigger extra API calls to fill it in automatically.
- Snowflake IDs (account, guild, channel, message, and user IDs) must remain exact digit strings end-to-end. Do not coerce 64-bit snowflakes into JavaScript `Number` values; JSON request/response fields should preserve them as strings.
- Repositories own persistence. Services own domain-level transactions and conflict handling. A UI should reload or reconcile after a mutation rather than pretend a request succeeded before the server confirms it.
- Database migrations are append-only once applied. Use repository APIs and migrations for persistent schema changes; do not mutate applied migration history.

## Dashboard and design-system architecture

The dashboard is a single-page application under `interfaces/dashboard/static/`, served by `interfaces/dashboard/server.py` and using REST plus WebSocket updates. The interface is intentionally a combination of bespoke framework views and builder-rendered plugin pages:

- Framework-owned, domain-heavy screens live in `static/js/views/` and use the shared API/store/render infrastructure.
- Plugin-authored pages use the safe builder contract in `core/dashboard_ui/` and its frontend renderer. Plugins do not inject arbitrary dashboard logic or gain access to internal UI state.
- The current navigation is an implementation inventory, not a permanent navigation contract. A design task may change page grouping, hierarchy, labels, or route presentation without changing backend semantics.
- Existing endpoints and views are evidence of current behavior, not a requirement to preserve an obsolete layout. Remove or rename an endpoint only when the associated task authorizes that API change and migration/compatibility is handled.

### Component contract

A reusable component is not complete when it only looks correct. Its contract should identify:

1. Responsibility and public inputs/outputs.
2. States and transitions: default, hover, focus, selected, disabled, loading, success, error, empty, and any domain-specific states that actually apply.
3. User actions and their consequences, including cancellation and recovery from errors.
4. Accessibility semantics, keyboard operation, focus behavior, status announcements, and reduced-motion handling where relevant.
5. Responsive constraints, overflow behavior, minimum hit area, and behavior at narrow viewports.
6. Data dependencies, API calls, loading/empty/error states, and the point at which a mutation is considered successful.
7. Tests for observable behavior and relevant regressions.

Do not require every component to implement irrelevant states solely to fill a checklist. State coverage should match the component's purpose and be explicit.

### Design tokens and themes

- The runtime theme system is part of the product. It supports theme presets, accent, radius, density, font choices, layout preferences, page token overrides, and custom CSS within its existing validation/allowlist boundaries.
- Shared components should consume canonical CSS custom properties rather than hard-coding one theme's colors and dimensions.
- A design proposal may specify a preferred default palette, spacing, radius, and typography, but those values must be reconciled with the runtime token map before implementation. Do not maintain two conflicting sets of “authoritative” tokens.
- A design-system change must preserve theme switching, density settings, and the separation between structure and theme values unless the approved task explicitly changes those capabilities.
- Treat screenshots/mockups as evidence of intended appearance and hierarchy, not as a complete specification of state, responsive behavior, or backend semantics.

### UI data and feedback

- Use the backend as the source of truth after mutations. Refresh or update the relevant store from returned server data/events; do not only change local text to suggest a successful save.
- Show loading, empty, failed, and recovery states for asynchronous sections.
- Keep field-level validation attached to the field, action failures near the action or in a durable page-level error region when appropriate, and transient informational feedback in a live region. Do not use a toast as the only carrier of a persistent or must-act error.
- Avoid full-page rerenders for high-frequency telemetry when a small in-place update suffices.
- Never silently drop reserved settings, scope constraints, unknown persisted data that must be forward-compatible, or user input on a failed save.

## Testing and change policy

- Unit tests should cover domain rules without Discord/network credentials.
- Integration tests should exercise the real composition over the simulator and verify cross-module contracts.
- Dashboard tests should verify user-observable behavior: the correct request, persisted result, loading/error recovery, keyboard behavior where relevant, theme compatibility, and responsive layout.
- Run focused tests first; then run the affected integration/acceptance tests. Run the broader suite when feasible and report any skipped tests and reasons. Do not declare a change fully verified based only on a structural grep or a test that checks for a comment.
- When a test encodes obsolete behavior that conflicts with an approved requirement, update the test and explain why; do not preserve a defect just to keep a test green.
- Keep changes scoped to the task and its necessary dependencies, tests, and docs. Do not overwrite unrelated work, switch branches, push, reset, or clean the repository unless explicitly authorized.

## Current implementation map

| Concern | Primary location |
|---|---|
| Composition and dependency wiring | `core/bootstrap.py` |
| Shared CLI/dashboard application API | `core/api.py` |
| Accounts and runtime cache | `core/account_manager/` |
| Plugin contract and dispatch | `core/plugin_base.py`, `core/plugin_manager/` |
| Settings resolution | `core/settings_manager.py` |
| Persistent sync membership | `core/sync_group_manager/` |
| Fleet policy and state transitions | `core/fleet/` |
| Pacing and server rate limits | `core/rate_limit_manager/` |
| Scheduled execution and retries | `core/task_scheduler/` |
| Adapter boundary and DTOs | `adapters/` |
| Persistence schemas and repositories | `database/` |
| CLI | `interfaces/cli/` |
| Dashboard HTTP/WS and SPA | `interfaces/dashboard/` |
| Plugin UI builder | `core/dashboard_ui/`, `interfaces/dashboard/static/js/render.js` |
| Operational per-channel plugin data | `database/plugin_data.py`, `state/plugin-data.db`, `ctx.services.data_*` |

## Boot and shutdown

`core/bootstrap.build()` is the composition root: it builds credentials/configuration, database and repositories, logging, event bus, rate limiter, settings, audit, scheduler, plugin management, account management, simulation and `ControlAPI`. It binds the scheduler sender after the account manager is ready, starts the scheduler, and exposes the framework through CLI, dashboard, or headless mode. Shutdown must reverse the active lifecycle safely and preserve logging/audit ordering.
