# LibzentSuite --- Application Architecture

**Revision:** 2.0\
**Date:** 2026-11-12\
**Status:** Design baseline\
**Scope:** Full application information architecture and application
shell\
**Repository changes:** None\
**Note:** Section 14 carries the binding engineering invariants ported
from the v1 architecture (2026-10-25); they remain in force alongside
this product specification.

## 1. Purpose

LibzentSuite is a multi-account Discord workspace and orchestration
application. It provides one control surface for managing account
identities, coordinating work across accounts, configuring plugins and
policies, observing events and execution, and operating the local
runtime.

This document defines the application-level structure and persistent
shell. It is a product/UI architecture specification, not a backend
implementation plan. It does not authorize or describe repository
modifications.

### Product principles

1.  One application supports multiple workspaces.
2.  Accounts are first-class resources with their own status, tasks,
    plugin state, activity, and supported configuration.
3.  Collections organize accounts; sync groups coordinate account
    execution. They are distinct concepts. *(Superseded 2026-11-13:
    the Workspace unification — `roadmap.md` — merges grouping and
    orchestration into one Workspace concept; this boundary holds
    only until W5 lands.)*
4.  Configuration uses global defaults with workspace- and account-level
    overrides.
5.  Operational state is observable and distinguishes configured intent
    from observed runtime state.
6.  The shell provides consistent navigation, workspace context, scope
    context, search, notifications, and runtime health.
7.  UI controls must reflect actual capabilities; unavailable or
    unimplemented behavior must not be presented as working.

## 2. Account organization model

``` text
LibzentSuite
└── Workspace
    ├── Accounts
    ├── Collections
    ├── Sync Groups
    ├── Workspace Policies
    ├── Workspace Plugin Configurations
    └── Workspace Activity
```

A workspace is the primary organizational boundary. Accounts belong to a
workspace. Collections are user-defined groupings and may provide
alternate views over workspace accounts. Sync groups are
execution-coordination resources, not visual collections.

  ------------------------------------------------------------------------------
  Resource                Responsibility                 Relationship
  ----------------------- ------------------------------ -----------------------
  Workspace               Organizational/configuration   Contains accounts and
                          boundary                       workspace-scoped
                                                         resources

  Account                 Connected Discord identity     Belongs to a workspace

  Collection              Flexible account               References accounts; an
                          organization/filtering         account may appear in
                                                         multiple collections

  Sync group              Coordinates work across        References
                          selected accounts              participating accounts
                                                         and coordination
                                                         behavior

  Task                    Unit of runtime work           Assigned to an account
                                                         or coordinated group,
                                                         subject to task
                                                         semantics

  Plugin                  Extension providing            Installed/configured at
                          capabilities                   supported scopes

  Policy                  Governs behavior               Defined globally and
                                                         optionally overridden
                                                         at narrower scopes
  ------------------------------------------------------------------------------

Workspace switching changes the active organizational context. It must
not silently change account enablement, task execution, or other runtime
state.

## 3. Configuration and management scope

### Scope hierarchy

``` text
Global
  └── Workspace
        └── Account
```

The default management entry point is **Global**. Users can move into
workspace or account scope for narrower configuration.

### Inheritance

The effective value is resolved from the narrowest scope that defines an
override:

1.  Account override, if present.
2.  Otherwise workspace override, if present.
3.  Otherwise global default.

For each inheritable setting, the UI must show the effective value,
whether it is inherited or overridden, the source scope, and an action
to create/edit/reset an override. Resetting an override restores
inheritance; it does not mutate the parent value.

  -----------------------------------------------------------------------
  Scope                   Applies to              Responsibilities
  ----------------------- ----------------------- -----------------------
  Global                  All applicable          Default policies,
                          workspaces/accounts     global rate-limit
                                                  policies, system-wide
                                                  plugin defaults, global
                                                  monitoring

  Workspace               Resources in one        Workspace policy
                          workspace               overrides, plugin
                                                  configuration,
                                                  workspace organization

  Account                 One account             Account policy/plugin
                                                  overrides and supported
                                                  account-specific
                                                  behavior
  -----------------------------------------------------------------------

Not every setting is necessarily overridable at every scope. Each
setting must declare its supported scopes.

**Scope context and operational selection are separate.** Scope
determines which configuration is being viewed or edited. Selection
determines which resources are targets of an operation. Changing scope
must not implicitly select accounts for bulk operations.

## 4. Full application information architecture

``` text
LibzentSuite
│
├── Workspace
│   ├── Workspace Overview
│   └── Workspace Settings
│
├── Fleet
│   ├── Overview
│   ├── Accounts
│   │   ├── Account Inventory
│   │   └── Account Detail
│   │       ├── Summary
│   │       ├── Tasks
│   │       ├── Plugins
│   │       ├── Sync Groups
│   │       ├── Policies
│   │       ├── Activity
│   │       ├── Plugin Data
│   │       └── Settings
│   ├── Collections
│   └── Account Groups
│
├── Orchestration
│   ├── Sync Groups
│   ├── Execution Plans
│   ├── Task Queues
│   ├── Schedules
│   └── Execution History
│
├── Extensions
│   ├── Plugin Catalog
│   ├── Installed Plugins
│   ├── Plugin Configuration
│   └── Account Plugin Data
│
├── Control Plane
│   ├── Policies
│   ├── Rate Limits
│   ├── Event Bus
│   ├── Activity
│   └── Logs
│
└── System
    ├── Local Instance
    ├── Storage and Databases
    ├── Connections
    └── Application Settings
```

### Module responsibilities

-   **Workspace:** workspace identity, switching, overview, and
    workspace-level settings.
-   **Fleet:** accounts in the active workspace, account
    lifecycle/status, collections, and account details.
-   **Orchestration:** sync groups, execution plans, task queues,
    schedules, and execution outcomes/history.
-   **Extensions:** plugin discovery and lifecycle visibility, installed
    plugins, plugin configuration, and account-scoped plugin data.
-   **Control Plane:** policy configuration and runtime observability
    for rate limits, event delivery, activity, and logs. It
    distinguishes configured policy from observed behavior.
-   **System:** local runtime, storage/database, connection, and
    application-wide settings.

### Account detail sections

  -----------------------------------------------------------------------
  Section                             Responsibility
  ----------------------------------- -----------------------------------
  Summary                             Identity, connection/runtime state,
                                      key metrics, recent activity

  Tasks                               Assigned tasks, queue membership,
                                      task history

  Plugins                             Plugins enabled/configured for the
                                      account

  Sync Groups                         Groups containing the account and
                                      its coordination role

  Policies                            Effective values and account
                                      overrides

  Activity                            Account-specific events and
                                      operations

  Plugin Data                         Account-scoped plugin data, subject
                                      to availability/permissions

  Settings                            Metadata and supported
                                      account-specific settings
  -----------------------------------------------------------------------

Global control-plane pages default to global scope but may provide
workspace/account filters. Workspace pages operate within the active
workspace. Account detail pages operate on one account.

## 5. Application shell architecture

### Shell responsibilities

The persistent shell owns: - Primary navigation and navigation state. -
Active workspace context. - Active configuration scope. - Global
search/command access. - Notifications and global activity entry
points. - Runtime/instance health visibility. - Page title, breadcrumbs,
and contextual actions. - Responsive navigation behavior. - Consistent
overlays, dialogs, and feedback surfaces.

The shell does not own module-specific business logic. Modules render
page content and page-level actions inside the shell's content outlet.

### Desktop frame baseline

  Element                             Initial specification
  --------------------------------- -----------------------
  Reference viewport                         1440 × 1000 px
  Expanded sidebar                                   256 px
  Collapsed sidebar                                   72 px
  Top application bar                                 64 px
  Main content horizontal padding                 28--32 px
  Main content vertical padding                   24--32 px
  Base spacing increment                               4 px
  Common spacing tokens                8, 12, 16, 24, 32 px

These are initial construction tokens, not measurements of a verified
Figma implementation.

### Shell structure

``` text
┌──────────────────────────────────────────────────────────────────┐
│                         Application Shell                        │
├────────────────┬─────────────────────────────────────────────────┤
│                │ Top Bar                                          │
│                │ [Nav Toggle] [Breadcrumbs] [Search]              │
│                │ [Scope] [Notifications] [Profile]                │
│                ├─────────────────────────────────────────────────┤
│ Sidebar        │ Page Header                                     │
│                │ [Title] [Description] [Contextual Actions]      │
│ Brand          │                                                 │
│ Workspace      │ Page Content                                    │
│ Switcher       │                                                 │
│ Navigation     │                                                 │
│ Groups         │                                                 │
│                │                                                 │
│ Instance       │                                                 │
│ Status         │                                                 │
│ Settings       │                                                 │
└────────────────┴─────────────────────────────────────────────────┘
```

### Sidebar

Contains product identity, workspace switcher, grouped route navigation,
active-route indication, expanded/collapsed behavior, local instance
status entry point, and settings.

Navigation groups: 1. Workspace 2. Fleet 3. Orchestration 4. Extensions
5. Control Plane 6. System

Collapsed navigation retains accessible labels through tooltips or
equivalent accessible naming.

### Top application bar

Contains sidebar toggle, breadcrumbs, global search/command palette
entry, current management scope selector, notifications entry
point/unread indication, and user/application menu. It should not
duplicate the workspace switcher if that is persistently available in
the sidebar.

### Scope context control

``` text
Global
Global / Workspace
Global / Workspace / Account
```

The selected scope must be explicit. Scope-aware pages identify
inherited values and their source. Unsaved edits must be protected when
scope changes.

### Page frame

Every route uses a consistent frame containing route
context/breadcrumbs, page title, optional description, contextual
actions, optional filters/view controls, main content, and page-level
feedback/state surfaces. Page content must not recreate the global
sidebar or top bar.

### Global status surface

Provide a consistent entry point for local application instance,
Discord/client connections, worker/runtime state, and background
operations. The shell may show an aggregate status; detailed diagnostics
belong in System and Control Plane. Status must support
unknown/unavailable states and reflect observed state rather than
requested intent.

## 6. Navigation and route model

The following is a conceptual route contract. Exact syntax can be
adapted to the existing router without requiring backend changes.

  Route                                   Page
  --------------------------------------- ---------------------------------------
  `/overview`                             Fleet overview
  `/workspaces`                           Workspace selection/management
  `/workspaces/:workspaceId`              Workspace overview
  `/workspaces/:workspaceId/settings`     Workspace settings
  `/accounts`                             Account inventory in active workspace
  `/accounts/:accountId`                  Account summary
  `/accounts/:accountId/:section`         Account detail section
  `/collections`                          Collections
  `/collections/:collectionId`            Collection detail
  `/orchestration/sync-groups`            Sync group inventory
  `/orchestration/sync-groups/:groupId`   Sync group detail
  `/orchestration/plans`                  Execution plans
  `/orchestration/queues`                 Task queues
  `/orchestration/schedules`              Schedules
  `/orchestration/history`                Execution history
  `/plugins/catalog`                      Plugin catalog
  `/plugins/installed`                    Installed plugins
  `/plugins/configuration`                Plugin configuration
  `/plugins/account-data`                 Account plugin data
  `/control/policies`                     Policies
  `/control/rate-limits`                  Rate limits
  `/control/events`                       Event bus
  `/control/activity`                     Activity
  `/control/logs`                         Logs
  `/system/instance`                      Local instance
  `/system/storage`                       Storage and databases
  `/system/connections`                   Connections
  `/settings`                             Application settings

Route access and authorization must follow the actual permission model;
this document does not assume a particular authentication
implementation.

## 7. Shared interaction contracts

### Workspace switching

-   Updates active workspace context and workspace-scoped resource
    lists.
-   Global routes remain global.
-   If the current resource is outside the new workspace, navigate to a
    safe landing page or explain the context mismatch.
-   Must not trigger runtime actions.

### Scope switching

-   Re-resolves configuration on scope-aware pages.
-   Updates effective values and inheritance sources.
-   Protects unsaved edits.
-   Does not alter operational target selection.

### Global search

Search supported resources: workspaces, accounts, collections, sync
groups, tasks, plugins, and relevant settings/policies. Results show
resource type and organizational context and must respect access
permissions.

### Bulk operations

-   Require an explicit selection set.
-   Display target count and target category.
-   Require confirmation for consequential/destructive operations.
-   Report per-target outcomes where supported.
-   Must not preserve ambiguous selections after workspace/scope
    changes.

### Notifications and activity

Notifications link to relevant resources or diagnostic context when
available. They are entry points, not replacements for activity/log
records.

## 8. Responsive shell behavior

  -----------------------------------------------------------------------
  Viewport                Navigation              Content
  ----------------------- ----------------------- -----------------------
  Wide desktop            Expanded persistent     Full layouts and dense
                          sidebar                 tables

  Compact desktop         Collapsible sidebar     Reduced gutters; retain
                                                  essential table columns

  Tablet                  Collapsed rail or       Progressively reduced
                          overlay drawer          columns

  Mobile                  Overlay navigation      Single-column layouts;
                          drawer                  table-to-card/list
                                                  alternatives
  -----------------------------------------------------------------------

Workspace and scope context must remain understandable at every size.
Important actions cannot depend on hover.

## 9. Shared design-system and component contract

### Token categories

-   Color: surface, elevated surface, text, muted text, border, accent,
    success, warning, danger, informational.
-   Typography: display, page title, section title, body, label,
    caption, numeric.
-   Spacing: 4 px base increments.
-   Radius: control, card, overlay.
-   Elevation: popover, menu, modal, floating surface.
-   Layout: sidebar widths, top-bar height, content maximum width, page
    gutters.
-   Motion: transition duration and reduced-motion behavior.

Exact color values and typography families are to be finalized in the
design system.

### Shared components

-   `ApplicationShell`
-   `Sidebar`
-   `NavigationGroup`
-   `NavigationItem`
-   `WorkspaceSwitcher`
-   `TopBar`
-   `Breadcrumbs`
-   `GlobalSearch`
-   `CommandPalette`
-   `ScopeSelector`
-   `ScopeInheritanceIndicator`
-   `NotificationCenter`
-   `InstanceStatus`
-   `PageHeader`
-   `ContextualActionBar`
-   `FilterBar`
-   `DataTable`
-   `ResourceList`
-   `ResourceCard`
-   `StatusIndicator`
-   `EmptyState`
-   `LoadingState`
-   `ErrorState`
-   `ConfirmationDialog`
-   `Toast` / `InlineFeedback`
-   `ResponsiveNavigationDrawer`

Components expose relevant loading, disabled, selected, error, and
unavailable states.

## 10. State and feedback requirements

  -----------------------------------------------------------------------
  State                               Required behavior
  ----------------------------------- -----------------------------------
  Initial loading                     Preserve layout; do not show false
                                      zero values

  Empty                               Explain what is empty; provide a
                                      relevant next action if one exists

  Loaded                              Display current data and its scope

  Partial failure                     Preserve available data and
                                      identify failed sections

  Error                               Explain failure and provide safe
                                      retry where appropriate

  Unavailable                         Distinguish
                                      unsupported/disconnected capability
                                      from empty result

  Permission denied                   Explain resource/action is
                                      inaccessible

  Stale data                          Indicate when displayed data is
                                      known to be out of date
  -----------------------------------------------------------------------

For runtime operations, distinguish desired state from observed state. A
requested connection is not proof that a connection succeeded.

## 11. Figma design sequence

1.  **Application shell:** desktop expanded/collapsed, workspace
    switcher, scope selector, search/command palette, notification
    panel, tablet/mobile navigation.
2.  Fleet overview.
3.  Account inventory, collections, and account detail.
4.  Sync groups and orchestration.
5.  Tasks, queues, schedules, and execution history.
6.  Plugin catalog, installed plugins, configuration, and account data.
7.  Policies, rate limits, event bus, activity, and logs.
8.  Local instance, storage, connections, and application settings.

Every module is designed against the shell contract. Modules must not
redefine global navigation or scope semantics.

## 12. Constraints and non-goals

-   No repository files are modified by this document.
-   No backend API, database schema, or runtime behavior is declared
    implemented.
-   The architecture does not assume every proposed page already has a
    backend endpoint.
-   It does not invent account permission roles or multi-user
    collaboration semantics.
-   Collections and sync groups remain distinct.
-   Configuration scope and operational selection remain distinct.
-   Figma implementation is separate and must be verified in the target
    file before being reported complete.

## 13. Decisions recorded

  -----------------------------------------------------------------------
  Decision                            Selected value
  ----------------------------------- -----------------------------------
  Account organization                Hybrid

  Workspace model                     Multiple workspaces supported

  Account organization                Accounts, collections, operational
                                      sync groups

  Management scope                    Hybrid

  Default management entry point      Global

  Configuration inheritance           Global → Workspace → Account

  Override behavior                   Explicit scoped override with
                                      reset-to-inherit

  First design deliverable            Application shell

  Repository modification             Not authorized

  Design implementation               Architecture specified; Figma edits
                                      not performed by this document
  -----------------------------------------------------------------------

## 14. Engineering invariants (binding — ported from the v1 architecture)

The sections above describe where the application is going. This section
carries forward the binding engineering contracts from the previous
architecture document (v1, 2026-10-25) so they remain in force during and
after the v2 product transition. It is the invariant summary; the
detailed module-ownership contract lives in `INTEGRATION.md`.

### Application boundary and composition

-   CLI and dashboard mutations go through the same `core.api.ControlAPI`
    facade. Extend the facade for legitimate features; never add a
    parallel interface-only business-logic path.
-   `core/bootstrap.py::build()` is the one composition root: it wires
    every manager, service, and adapter. Modules communicate through
    their contracts and injected dependencies — no service locators,
    hidden imports across boundaries, or global mutable wiring.
-   Transport layers (REST handlers, WS, CLI parsing) validate input and
    serialize output; they never reimplement domain rules.

### Plugins

-   Plugins are independent of the interface layer: they own no accounts,
    pacing, database connections, or dashboard/CLI state. All capability
    access goes through `PluginContext` / `PluginServices` (`ctx.services`).
-   Python plugins are NOT sandboxed; safety comes from the explicit
    services surface, not isolation.
-   Core stays game-agnostic — game-specific strings and recognition
    rules belong in parser packs and plugin code, never in `core/`.

### Safety and data semantics

-   The rate-limit floor is a safety invariant: no configured action
    interval below `MIN_ACTION_INTERVAL = 1.5` s (cap `MAX_INTERVAL =
    86400` s). Server-provided rate limits (429/Retry-After) are always
    respected. Fleet policy may allow, block, or defer — it can never
    weaken or bypass pacing.
-   Discord snowflakes (account, guild, channel, message, user ids)
    remain exact digit strings end-to-end: Python, JSON, persistence,
    logs, and browser state. Never `Number` in JavaScript.
-   Credentials are write-only in the UI, encrypted at rest (Fernet),
    with the master key outside the database. Tokens never render into
    logs, API responses, or exception messages.
-   Outbound actions follow the single pipeline: `PluginServices` →
    `TaskScheduler.submit` → one in-flight action per account →
    `RateLimitManager.acquire` → final validation → sender hook →
    adapter → audit/history/events. Retries only for scheduler-classified
    transient errors; timeouts are terminal (never auto-retried — an
    in-flight send cannot be un-sent).

### Account, plugin, and data models

-   Capability model is direct `account_plugins` assignment (fail-closed:
    no assignments → no plugins run). Do not reintroduce the removed
    ProfileManager or its `profiles`/`account_profiles` tables.
-   `plugin_profiles` = named per-plugin settings presets. AccountPluginData
    (`state/plugin-data.db`) = operational documents with the fixed scope
    lattice account → plugin → guild → channel, standalone copies,
    optimistic version writes. These are distinct stores; never merge
    them or add channel-document inheritance.

### Groups and fleet orchestration

-   Sync groups (persistent membership + pacing coordination) and Fleet
    orchestration (task ownership, eligibility, transitions) are distinct
    concepts with distinct persistence. Never merge them.
-   `core/fleet/transitions.py` is the single transition-validator source;
    the dashboard never reimplements transition tables. Observations
    authorize nothing by themselves — restrictions only through validated
    service operations. Generation/version fences reject stale
    assignments. The reconciler surfaces ambiguity instead of guessing.
    Handoff validates the destination before revoking the source; a
    failed destination check must never destroy the source assignment.
    `resume_task` requeues a suspended task to QUEUED (UI copy says
    "Requeue").

### Events, persistence, dashboard

-   Typed events communicate observations; an event is never
    authorization for an operation. Raw library objects convert to
    immutable DTOs once at the adapter boundary and never leak further.
-   Repositories own persistence; services own domain transactions.
    Applied migrations are append-only. `Database(":memory:")` must stay
    usable by tests/simulation.
-   The dashboard is the SPA under `interfaces/dashboard/static/` plus
    plugin pages through the safe builder contract (`core/dashboard_ui/`)
    — plugins never gain arbitrary DOM, backend internals, or app state.
    After a mutation, the server's confirmed state is the source of
    truth (re-fetch; never fake success locally). The runtime theme
    engine (presets, density, fonts, custom CSS) is a product feature —
    shared components consume canonical CSS custom properties, not
    hardcoded theme values.
-   A dashboard page mounting, unmounting, or failing must never stop or
    start backend automation.

### Lifecycle and testing

-   `build()` boots the whole stack; `Framework.shutdown()` reverses it
    in a fault-tolerant ordered teardown (scheduler drain → periodic
    tasks stopped → persistence queues drained → plugin manager →
    plugin-data store → database). Every step runs even if an earlier
    one failed.
-   Tests: deterministic (injected clocks, in-memory databases, simulator
    adapters); no real credentials or network in routine tests; focused
    suites first, then adjacent, then the full suite. Never weaken a test
    to reach green.

------------------------------------------------------------------------

**End of architecture document.**
