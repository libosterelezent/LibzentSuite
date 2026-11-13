# LibzentSuite — Integration Contract

Read this before changing module boundaries, cross-module behavior, or the dashboard integration. This document defines stable integration invariants and responsibilities. Current function signatures and detailed implementation behavior are defined by source code and its tests; do not copy old signatures forward if they no longer match the implementation.

## 1. Authority, scope, and conflict resolution

### What this document binds

This contract binds module boundaries and integration behavior. It does **not** freeze the dashboard's current page layout, every endpoint name forever, or the implementation strategy used by a previous phase.

Use this conflict-resolution order:

1. **Explicitly approved task requirements** define the intended change.
2. **Stable architecture invariants in `ARCHITECTURE.md` and this contract** define boundaries that the change must preserve unless the task explicitly changes them.
3. **Current source code and current tests** establish how the system behaves today.
4. **Current schemas/configuration and API responses** establish stored data and runtime contract details.
5. **Historical plans, examples, stale comments, and prior-phase descriptions** are context only; they must not override current requirements or source evidence.

If a current test encodes behavior that conflicts with an approved requirement, update the test along with the implementation and explain the changed expectation. If two requirements materially conflict, do not silently pick whichever is easiest to implement. State the conflict and follow the approved product requirement; routine technical choices may be resolved independently and documented briefly.

### Ground rules

- Python runtime: Python 3.10 or newer. Production runtime dependencies are those declared in `requirements.txt`; `discord.py-self` is a live-mode dependency.
- Paths in this document are relative to the repository root.
- Public functions/classes need docstrings describing purpose and important errors/side effects.
- Validate untrusted input at boundaries. Domain/API errors should be human-readable; malformed input must not crash the process with an uncaught exception.
- Keep work scoped to the requested change and files needed for its dependencies/tests/docs. Do not overwrite unrelated work. Do not push, reset, clean, or switch branches unless separately authorized.
- Account tokens are encrypted in the database. `.env` is for IDs/auth switches and other appropriate configuration, not plaintext account tokens. Never return token values to the UI or logs.
- No configured rate policy may go below `MIN_ACTION_INTERVAL = 1.5` seconds/action. Intervals and the floor are capped at `MAX_INTERVAL = 86400.0`. Server-provided rate limits are always respected.
- A task may authorize changes across several related modules. Do not treat an initially mentioned file list as a ban on necessary contract, test, or frontend changes.

## 2. Architectural boundary

```text
                         CLI                  Dashboard
                          |                      |
                          +----------+-----------+
                                     |
                                     v
                            core.api.ControlAPI
                                     |
                   domain services / managers / repositories
                                     |
         +---------------------------+---------------------------+
         |                           |                           |
      EventBus                 TaskScheduler               FleetService
         |                           |                           |
         |                    RateLimitManager             FleetPolicy
         |                           |                           |
         +---------------------------+---------------------------+
                                     |
                           account sender / adapter
                                     |
                        Discord API or simulator
```

`core/bootstrap.py` is the composition root. It constructs dependencies and supplies them to modules; business modules should not recreate composition logic or import interface internals to obtain dependencies.

### Shared application API

- `core.api.ControlAPI` is the shared facade used by CLI and dashboard.
- Interface handlers parse transport input, call `ControlAPI`, and serialize its result. They must not reimplement business rules, reach into manager internals, or write to repositories directly.
- If a legitimate feature needs an application operation that does not yet exist, **extend `ControlAPI` and its tests** and wire the operation through each required interface. The facade is not immutable. Do not work around the contract with a dashboard-only shortcut.
- Keep business results transport-neutral. HTTP status codes, JSON envelopes, CLI strings, and visual state belong to their respective interface adapters.

### Plugins and services

Plugins use `PluginContext` / `PluginServices` via `ctx.services`. They must not manipulate accounts, databases, dashboard/CLI state, shared rate limiting, or fleet membership directly. Their outbound capabilities go through supported services and the central scheduling/pacing pipeline. New supported plugin capabilities must be deliberately added to the plugin contract and tested.

The framework core remains game-agnostic. Game-specific command strings, locales, and message-recognition rules belong in parser packs and plugin/workload code. Core imports must not depend on `parsers/`.

## 3. Current module ownership

| Area | Owns | Must not absorb |
|---|---|---|
| `core/bootstrap.py` | dependency construction, wiring, lifecycle composition | domain decisions that belong in services |
| `core/api.py` | shared application operations and validation orchestration | HTTP/DOM details or interface-specific business logic |
| `core/account_manager/` | account lifecycle, runtime client/cache, sender hook | persistent sync-group ownership or rate-policy calculation |
| `core/plugin_base.py`, `core/plugin_manager/` | plugin contract, discovery, per-account attach/dispatch/state | dashboard state, direct DB access from plugins, pacing bypasses |
| `core/settings_manager.py` | schema validation and settings resolution | UI rendering or component state |
| `core/sync_group_manager/` | persistent sync-group rows and account membership *(deprecated 2026-11-13 — superseded by the Workspace unification, `roadmap.md`; removed in W5)* | fleet task ownership or rate-limit execution timing |
| `core/fleet/` | fleet membership projection, policy, task/member transitions, handoff, cooldown decisions, reconciliation *(deprecated 2026-11-13 — superseded by the Workspace unification, `roadmap.md`; removed in W5)* | Discord API requests and execution pacing |
| `core/task_scheduler/` | task queue, execution lanes, retries, cancellation, history lifecycle | choosing which fleet member is eligible or bypassing pacing |
| `core/rate_limit_manager/` | action pacing, shared clocks, cooldowns/server 429s, warnings | choosing task ownership or interpreting game messages |
| `core/event_bus.py` | in-process typed event delivery | durable state truth or authorization of commands |
| `core/audit.py`, `core/logging_service.py` | audit trail and structured logs | secret disclosure or business-state ownership |
| `database/` | schemas, migrations, repository persistence | interface behavior or UI-specific transformations |
| `adapters/` | Discord/simulator boundary and data conversion | core policy decisions |
| `interfaces/cli/` | command parsing, help, presentation, calls to `ControlAPI` | separate business rules |
| `interfaces/dashboard/` | HTTP/WS transport, SPA, view state and presentation | direct manager/repository operations or Discord API calls from view code |
| `core/dashboard_ui/` | safe plugin page/component contracts, visibility checks, data/action dispatch | arbitrary HTML/script injection or framework internals exposure |

## 4. API boundary, external calls, and cache

The framework does not make Discord API calls merely to prefetch or decorate its own UI. Discord calls are on-demand and belong to supported service/action flows. `PluginServices` includes supported messaging and other action methods; current exact methods and signatures are in `core/plugin_base.py` and its implementation.

This restriction concerns **Discord/external-platform API calls**. It does not prohibit the dashboard from requesting data from its own REST API, fetching a view's data on demand, refreshing after a mutation, or consuming WebSocket events.

- Discord runtime status and cached guild/channel/user information should be drawn from the live client's cache snapshot when available.
- Missing optional data stays absent until an operation explicitly needs and is authorized to fetch it. Do not add implicit fetch loops to improve visual completeness.
- The adapter converts raw library events into framework DTOs once at the boundary. Raw runtime-only objects must not be persisted or logged.
- `fetch_user` and DM-channel resolution use the existing account-scoped cache where applicable. Do not duplicate cache logic in views or plugins.
- Discord snowflakes remain exact digit strings across browser state, JSON payloads, Python processing, persistence, and logs. Never use JavaScript `Number` to hold a Discord snowflake.

## 5. Outbound task and pacing contract

```text
PluginServices request
    -> TaskScheduler.submit(...)
    -> one in-flight action per account
    -> RateLimitManager.acquire(action, account, plugin, priority)
    -> final account/cache/policy validation
    -> AccountManager sender hook
    -> DiscordClient or SimClient
    -> history + audit + event publication
```

### Task scheduling

- Outbound supported actions use the scheduler rather than calling the Discord client directly from plugins or interfaces.
- The scheduler has one active worker lane per account; cross-account scheduling may provide priority fairness.
- Cancellation is cooperative where an action is already in flight. Terminal bookkeeping must not duplicate history/audit/event records.
- Retries are limited to errors that the scheduler classifies as transient. A policy block or validation failure fails with the reason rather than retrying blindly.
- When a task is admitted to a queue, it must be checked again when dispatching if current policy/state may have changed.

### Rate limiting

- `RateLimitManager` is the only authority for execution timing, pacing intervals, jitter, and server cooldowns.
- Applicable policies compose by taking the most restrictive interval. Preserve the supported global, account, plugin, and account+plugin scopes.
- SyncGroupManager feeds account-to-group membership to the limiter. Persistent membership remains owned by SyncGroupManager; runtime timing remains owned by the limiter.
- A Discord 429 is reported to the limiter, including retry-after and whether the limit is global, before retrying.
- The safety floor cannot be bypassed by UI settings, plugins, fleet policy, or simulation shortcuts.
- Fleet policy may allow, block, or defer an action. It cannot alter the action's rate-limit grant.

## 6. Event and fleet contracts

`core/event_bus.py` provides typed in-process events. Events communicate observations and state changes, but an event by itself does not authorize a requested operation. Durable domain changes must go through the owner service/repository path and be validated before being published as authoritative state.

### Fleet

- `FleetService` is the authoritative writer for supported fleet operations.
- `core/fleet/transitions.py` is the transition-validation source. Do not reimplement transition tables in the dashboard.
- `FleetPolicy` checks whether the action is eligible at dispatch time. The scheduler integrates the dispatch guard; the limiter remains responsible for timing.
- Parser or message observations cannot directly grant work. An observation can trigger a restriction through validated service operations.
- Generation/version checks reject stale assignments. Reconciliation must be idempotent and repair only safe states; ambiguous cases should be surfaced.
- Handoff validates the destination before revoking the source. A failed destination check must never destroy the source assignment.
- Sync-group membership and fleet membership are distinct concepts. Do not merge their persistence responsibilities.

The API operation named `resume_task` requeues a suspended task to `QUEUED`; UI copy should say “Requeue” or “Return to queue” where that better represents the actual effect. Labels should be determined by real state transitions, not by method names alone.

## 7. Persistence and schema changes

- Use repository APIs for persistent writes. Do not let plugins or interface handlers open database connections or issue direct SQL.
- Applied migrations are append-only; create a new migration for a schema change. Do not rewrite applied migration history.
- Distinguish settings presets from operational plugin data. `plugin_profiles` is for named per-plugin settings bundles; `state/plugin-data.db` stores AccountPluginData documents.
- AccountPluginData has a fixed scope lattice: account → plugin → guild → channel. Documents are standalone copies, not inherited overlays. Writes use optimistic versions and must report conflicts rather than silently overwriting newer data.
- Scope/permission settings must be preserved when editing unrelated fields. If a complete settings object is sent to an API, it must include all reserved fields that the caller is responsible for preserving or explicitly editing. A UI must not turn a restricted scope into an unrestricted one through omission.
- Preserve valid unknown fields when a contract explicitly requires forward compatibility. Do not casually discard unrecognized stored data in a read-modify-write flow.
- Validate input types at API boundaries. Boolean fields must not accept arbitrary truthy values; IDs that are strings must not be silently converted to imprecise numeric values.

## 8. Current dashboard integration

The dashboard lives in `interfaces/dashboard/` and consists of the HTTP/WebSocket server plus a no-build SPA under `interfaces/dashboard/static/`. REST operations map to `ControlAPI`; live state is propagated to the SPA through the shared store and WebSocket frames.

### Navigation and page ownership

The shipped v3 interface currently includes Home, Accounts, Activity, Fleet, Policies, Admin, Tools, and plugin-related registry/builder surfaces. This is a **current inventory only**, not a mandatory navigation blueprint. A UI task may regroup or rename screens, change navigation hierarchy, or introduce a new view when required by the approved design. It must preserve the underlying operations and state semantics unless the task explicitly changes them.

- Bespoke framework views live in `interfaces/dashboard/static/js/views/`.
- Shared app shell/navigation lives in `shell.js`; routing and mounting live in the shared router; view state uses the common store and API layer.
- Shared low-level UI utilities are in `ui.js`; generic builder rendering lives in `render.js` and the dashboard UI client module. Inspect current code before declaring helpers duplicated or proposing a consolidation.
- The server's actual route table and `core/api.py` are the current endpoint evidence. Old route lists in historical plans are not binding if the implementation has since evolved.
- Do not remove or rename public routes merely to fit a new screen layout. When an API change is required, update the application facade, server route, frontend caller, tests, and documentation together.

### Theme and token contract

The theme engine is a supported product feature, not optional decoration. It supports presets, accent, radius, density, font choices, layout preferences, page token overrides, and custom CSS under its current validation and allowlist boundaries.

- Reusable components consume the runtime CSS variables and shared classes wherever practical. Avoid hard-coded colors/dimensions in reusable UI that bypass the theme.
- A design reference may propose a target palette or dimensions, but it does not become the runtime token contract until the tokens are reconciled and implemented. There must be one coherent mapping between the design specification, CSS tokens, and theme engine.
- Do not silently remove theme settings or reduce them to one fixed palette because a mockup shows one visual state.
- If a token file is consumed as JSON, it must be valid JSON with unique keys. Validate it with a parser; duplicate-key JSON objects are not acceptable.

### Component and interaction contract

Every reusable component or important view interaction should be designed as behavior plus appearance. Before declaring it complete, define applicable states and test them:

- Inputs: label/help, parsing/validation, disabled state, invalid state, preserved value on failure, and correct value collection.
- Buttons/actions: enabled/disabled, loading/busy without destructive child replacement or layout jump, success/error feedback, and repeat submission behavior.
- Dialogs/drawers: labelled semantics, open/close lifecycle, Escape behavior where appropriate, focus movement/restoration, keyboard containment when applicable, and narrow viewport fit. Prefer correct native semantics when available; do not add a custom focus trap solely because a helper lacks a particular code pattern.
- Tabs/menus: selected state, keyboard navigation, focus, and lazy loading when data is expensive.
- Tables/lists: empty/loading/error states, stable identity, overflow strategy, accessible headers/labels, and available row actions consistent with the backend state machine.
- Async views: cancellation/superseded-navigation protection, loading/empty/error/retry behavior, server-confirmed save semantics, and clean subscription disposal.
- Feedback: inline errors for fields/actions where actionable, persistent/banner errors for conditions that remain, and live-region announcements for status changes where necessary. A transient toast must not be the only carrier of an important persistent error.
- Text overflow: do not truncate critical IDs, statuses, error messages, or links without a way to reveal the full value; avoid hover-only disclosures for keyboard users.

Only require states that make sense for a component's responsibility. The acceptance requirement is observable behavior, not a source comment or a specific helper name.

### Responsive and accessible by default

- Responsive behavior must be considered during component design, not postponed until after desktop styling.
- Verify realistic desktop, tablet, and narrow mobile widths; no unintended horizontal overflow, clipped primary actions, or unusable dialogs.
- Preserve visible keyboard focus, semantic controls, accessible names, correct expanded/selected/busy states, reduced-motion support, and contrast appropriate to the active theme.
- Hit areas should meet the agreed accessible target even when the visible icon/control is smaller. Distinguish visual size from interactive hit area.
- Use live regions for state changes that otherwise would be invisible to assistive technology; avoid making the entire page a noisy announcement region for high-frequency telemetry.

### UI/backend behavior

- Use the existing API client and store. Do not call managers or repositories directly from JavaScript.
- Show loading, empty, failure, and retry/recovery states for async data.
- Consider a mutation successful only after the backend confirms it. Render returned server state or re-fetch the relevant data after mutation.
- Keep user input on failed submissions and display the cause near the relevant control when possible.
- Avoid rebuilding entire views at telemetry frequency when small in-place updates suffice.
- Do not drop `settings.channels`, account/plugin associations, permission/scope fields, or other reserved data simply because an editor is displaying only a subset of settings.
- Ensure multiple editors of the same domain object uphold the same data-preservation rules, even if their layout differs.

## 9. Current public integration surfaces

This section records the stable responsibilities and key method families. Exact signatures should be verified against source before changing call sites; a signature shown in old documentation does not override live source.

### Logging and audit

- `core/logging_service.py` provides structured records, recent tails, subscriptions, and optional persistence attachment.
- `core/audit.py` records interaction/action history and the timing/attempt information associated with scheduled outbound work.
- Never log plaintext tokens or raw runtime-only Discord objects. Redact sensitive fields before rendering raw settings or diagnostic views.

### Settings and plugins

- `core/settings_manager.py` owns setting schemas, validation, default/global/account layering, effective resolution, and mutation semantics.
- `core/plugin_base.py` defines `PluginBase`, context, services, and lifecycle hooks. `plugins/` may be empty in production; fixtures under `tests/fixtures/` are test-only.
- `core/plugin_manager/` owns discovery, per-account plugin instances, dispatch, activation/deactivation, enabled state, and manifest generation. A plugin setting mutation goes through the settings/application APIs.
- The supported plugin API is the documented `PluginServices` surface and other explicitly exported types. Internal core helpers are not implicitly supported just because they are importable.

### Rate limiting

`core/rate_limit_manager/` owns policy storage integration, effective-policy calculation, pacing, shared group clocks, server cooldowns, the floor, warnings, statistics, and the sequential lane where that feature is supported. Policy precedence is restrictive: the effective interval must not be faster than any applicable policy demands. Do not move fleet ownership or parser interpretation into this module.

### Scheduler

`core/task_scheduler/` owns task submission, execution lanes, priority, cancellation, retry/backoff, timeouts, and history. `AccountManager` supplies the sender hook. Keep the scheduler and limiter separate: the scheduler determines when work is dispatched; the limiter grants timing permission for the outbound action.

### Account management

`core/account_manager/` owns account lifecycle, connection supervisors, runtime state, cache snapshots, plugin services implementations, and the final sender hook. Runtime details should be sourced from the live client/cache. Account plugin assignment is direct; do not reintroduce removed account-profile indirection.

### Adapters

`adapters/base.py` defines the client boundary. `adapters/discord/` supplies the live client and imports the Discord library lazily. `adapters/simulator/` provides the in-memory world/client and scenario runner. Raw adapter event data is converted to framework DTOs once and should not leak beyond the adapter boundary.

### CLI

`interfaces/cli/` parses subcommands and formats output. It calls `ControlAPI` only. The command surface may evolve; keep strict argument parsing, useful help/error messages, and parity with application operations.

### Dashboard server and REST/WS

`interfaces/dashboard/server.py` maps REST requests to `ControlAPI`; `interfaces/dashboard/ws.py` transports live updates. Preserve authentication, validation, request-body limits, explicit error/status semantics, and safe serialization. A new route needs a defined request shape, response/error contract, and tests. Route changes must not smuggle business logic into the server.

### Database

`database/schema.py` owns ordered migrations; `database/repositories/` (plus the dedicated plugin data store) owns persistence APIs. `Database(":memory:")` must remain usable by simulation/testing. Operations must not block the event loop with synchronous database work.

## 10. Data model notes

- `account_plugins` is the direct account→plugin assignment record set.
- `plugin_states` represents per-account enabled/disabled plugin state; assignment is not enablement.
- `plugin_profiles` contains named plugin settings presets and account bindings.
- `state/plugin-data.db` contains AccountPluginData operational documents, separate from settings presets.
- `sync_groups` owns persistent sync group records; `accounts.sync_group` expresses one-group membership.
- Fleet group/task assignment, eligibility, cooldown, and history concepts are stored through the fleet repositories/service. They are not the same thing as sync groups or rate-limit policies.
- Never assume data is available merely because an object is absent from the UI. Use the source API contract to determine whether the value is unknown, empty, disabled, or missing.

## 11. Testing and acceptance

### Expected validation

1. Add/update unit tests for changed domain logic and input validation.
2. Add/update integration tests for cross-module behavior when APIs, persistence, scheduling, or events change.
3. Add/update dashboard acceptance tests for user-visible behavior: API payloads, server-persisted results, error preservation, loading/empty/retry states, component state changes, and relevant accessibility/responsive behavior.
4. Run the focused tests first, then affected integration tests and broader relevant suites when feasible. Record failures honestly; distinguish a pre-existing unrelated failure from one introduced by the change.

Do not use a structural grep alone as proof of user-visible behavior. Do not assert that focus containment works merely because a focus-trap helper exists; test keyboard behavior. Do not add a brittle requirement that a comment or particular implementation name must be present if equivalent behavior is correct.

### Simulator and determinism

Use `adapters/simulator/` for tests that need the application stack without a live Discord connection. Inject clocks/seeds where the code supports it. Tests should avoid real credentials and network access unless a specific live acceptance test is deliberately being performed.

### Definition of done

A cross-module/UI change is done when:

- behavior matches the approved requirement;
- the proper owner module enforces its invariants;
- CLI and dashboard use the same application capability where applicable;
- error, empty, loading, and recovery cases are covered where they apply;
- tests cover relevant regressions and report any skipped validation;
- documentation reflects the current behavior rather than a previous phase;
- the diff contains only task-related changes and has been reviewed.

## 12. Current implementation map

| Concern | Primary location |
|---|---|
| Composition root | `core/bootstrap.py` |
| Shared application facade | `core/api.py` (`ControlAPI`) |
| Account lifecycle/runtime/cache | `core/account_manager/` |
| Plugin contract and dispatch | `core/plugin_base.py`, `core/plugin_manager/` |
| Settings schema/resolution | `core/settings_manager.py` |
| Persistent sync group membership | `core/sync_group_manager/` |
| Fleet model/service/policy/reconciliation | `core/fleet/` |
| Pacing and server limits | `core/rate_limit_manager/` |
| Task queue/execution | `core/task_scheduler/` |
| Event delivery | `core/event_bus.py` |
| Adapter boundary and rich message DTOs | `adapters/` |
| Database schemas/repositories | `database/` |
| Operational plugin data | `database/plugin_data.py`, `state/plugin-data.db`, `ctx.services.data_*` |
| CLI | `interfaces/cli/` |
| Dashboard server and SPA | `interfaces/dashboard/` |
| Safe plugin UI builder | `core/dashboard_ui/`, `interfaces/dashboard/static/js/render.js` |
| Shared frontend utilities/theme/router/store | `interfaces/dashboard/static/js/` and `static/styles/` |

This map is an entry point, not an exhaustive dependency specification. Verify the actual source tree before adding, renaming, or removing a module.
