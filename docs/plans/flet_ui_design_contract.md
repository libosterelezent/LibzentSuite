# LibzentSuite — Flet Interface Design & Implementation Contract

**Target repository:** `libosterelezent/LibzentSuite`  
**Target branch:** `feat/pivot-core`  
**Interface framework:** Flet  
**API framework:** FastAPI  
**Application facade:** `core.api.ControlAPI`  
**Contract type:** UI architecture, design, interaction, and implementation requirements

---

# 1. Mission

Design and implement a complete, maintainable Flet interface for LibzentSuite.

The previous custom SPA dashboard has been removed. Flet is now the designated UI framework. The interface must be designed around Flet's native Python control model rather than attempting to reproduce the previous HTML/CSS/JavaScript implementation.

The goal is not simply to create a visually attractive dashboard. The interface must provide a coherent operational control surface for the entire LibzentSuite application.

It must allow the operator to:

- Manage multiple Discord accounts.
- Organize accounts into workspaces.
- Assign and manage plugins independently for each account.
- Configure account-owned plugin profiles.
- Inspect workspace presets and coordinated execution.
- Monitor tasks, execution state, cooldowns, and rate limits.
- Inspect logs, audit records, and runtime events.
- Manage system-wide configuration and safety controls.
- Observe operational state without interfering with the execution engine.

The interface must remain a presentation and interaction layer. It must never become another implementation of the application core.

# 2. Binding architecture

## 2.1 Application architecture

The application follows this structure:

```text
                 LibzentSuite
                      |
                Core Bootstrap
                      |
                 ControlAPI
                 /        \
                /          \
          CLI Interface   FastAPI
                             |
                       REST / WebSocket
                             |
                          Flet UI
```

Responsibilities:

| Component | Responsibility |
|---|---|
| `core/bootstrap.py` | Constructs and owns the application runtime |
| `core/api.py` | Shared application facade and operation boundary |
| `interfaces/api/` | HTTP endpoints, request validation, authentication, WebSocket transport |
| `interfaces/ui/` | Flet presentation, navigation, controls, user interactions |
| `interfaces/cli/` | Terminal-based interface |
| `core/` | Domain logic, orchestration, lifecycle, and business rules |
| `database/` | Persistence and repositories |

Both interfaces must use the same `ControlAPI` operations.

The Flet application must not construct a second set of managers or create an independent application runtime.

## 2.2 Strict ownership rules

The following are prohibited:

- Direct database connections or SQL from Flet pages.
- Importing internal managers into UI page modules to perform operations.
- Reimplementing account, plugin, workspace, or scheduler business logic.
- Bypassing `ControlAPI` to manipulate runtime state.
- Maintaining an independent copy of authoritative application state.
- Calling Discord APIs directly from UI controls.
- Implementing separate validation rules that contradict the backend.
- Treating WebSocket events as permission to perform operations.

The UI may perform presentation-level validation, but the backend remains authoritative.

## 2.3 FastAPI boundary

FastAPI is the communication layer between the Flet application and LibzentSuite.

The UI should consume API operations rather than accessing the core directly.

All requests must have:

- Explicit request and response contracts.
- Consistent error handling.
- Bounded request timeouts.
- Authentication.
- Appropriate loading and failure states.
- Predictable serialization.

The existing `X-API-Token` authentication mechanism must be preserved and applied consistently, including WebSocket connections.

The WebSocket implementation must eventually deliver actual application events rather than relying on periodic status polling as its primary update mechanism.

# 3. Flet implementation principles

## 3.1 Native Flet first

Use Flet's native controls and layout primitives.

Preferred building blocks include:

- `Page`
- `Row`
- `Column`
- `Container`
- `NavigationRail`
- `NavigationBar`
- `Tabs`
- `DataTable`
- `ListView`
- `Card`
- `Dialog`
- `AlertDialog`
- `TextField`
- `Dropdown`
- `Checkbox`
- `Switch`
- `ProgressRing`
- `ProgressBar`
- `SnackBar`
- `IconButton`
- `FilledButton`
- `OutlinedButton`

Do not recreate the former SPA architecture inside Flet.

Avoid unnecessary embedded HTML, custom JavaScript, browser DOM manipulation, and WebView-based components.

Custom controls are acceptable when native controls cannot adequately represent a required interaction.

## 3.2 Component architecture

Organize the UI around reusable components and independently maintainable pages.

Proposed package organization:

```text
interfaces/
  ui/
    __init__.py
    app.py
    config.py
    client/
      api_client.py
      errors.py
      models.py
      websocket.py
    shell/
      navigation.py
      layout.py
      context.py
      notifications.py
    components/
      cards/
      dialogs/
      forms/
      tables/
      status/
      feedback/
    pages/
      overview/
      accounts/
      workspaces/
      plugins/
      tasks/
      rate_limits/
      activity/
      settings/
    state/
      session.py
      subscriptions.py
    theme/
      tokens.py
      light.py
      dark.py
```

This is a logical organization, not a requirement to create empty directories or unnecessary abstraction layers.

Each module should have a clear responsibility.

Page modules should not contain the implementation of shared API clients, global theme management, or application lifecycle management.

## 3.3 Page registration

Preserve the idea of a page registry, but make registration deterministic and import-safe.

Requirements:

- Each page has a unique route or identifier.
- Each page has a clear title and navigation label.
- Navigation metadata must not be duplicated across unrelated modules.
- Page construction must not trigger unexpected network requests.
- Page state must be scoped to the relevant application session.
- Rebuilding a page must not leak event subscriptions.
- Navigation must not cause duplicate listeners or background tasks.

Avoid mutable global objects for user-specific or session-specific UI state.

# 4. Application shell and information architecture

The shell must provide a consistent navigation model and a persistent operational context.

## 4.1 Primary navigation

Use the following conceptual navigation structure:

1. Overview
2. Accounts
3. Workspaces
4. Plugins
5. Tasks
6. Rate Limits
7. Activity
8. Settings

Each section represents a functional area of the application.

Do not turn every operation into a separate top-level navigation entry. Related functionality belongs within its appropriate section.

## 4.2 Navigation behavior

The shell must support:

- Clear active-page indication.
- Predictable navigation history.
- Page-specific loading indicators.
- Error handling without destroying the entire shell.
- Preservation of appropriate page state when navigating away and returning.
- Responsive navigation based on available window space.

The navigation system must not rebuild unrelated application sections unnecessarily.

## 4.3 Global operational context

The application must make the current management context visible.

The context can identify:

- Global application scope.
- Selected workspace.
- Selected account.
- Current operational target selection, where applicable.

Important distinction:

**Management scope and operational target selection are different concepts.**

Changing the currently viewed workspace must not silently change the accounts selected for an operation.

Likewise, viewing an account must not imply that a bulk action is authorized for every account in its workspace.

Any action affecting multiple accounts must explicitly identify its targets before execution.

# 5. Functional page contracts

## 5.1 Overview

Purpose: provide an operational summary of LibzentSuite.

The overview should expose information such as:

- Application runtime status.
- Number of configured accounts.
- Running, stopped, and unavailable accounts.
- Workspace status.
- Active and queued tasks.
- Failed tasks and recent failures.
- Current rate-limit warnings.
- Recent audit and runtime activity.
- Emergency-stop status.

The overview must distinguish configured state from observed runtime state.

For example:

- Configured account does not mean connected account.
- Assigned plugin does not mean enabled plugin.
- Enabled plugin does not necessarily mean currently executing.
- Missing runtime information must not be presented as a successful state.

Avoid presenting fabricated statistics or metrics that are not available through the API.

## 5.2 Accounts

Purpose: manage individual Discord accounts.

The account list should expose:

- Account name.
- Runtime status.
- Workspace membership.
- Assigned plugins.
- Relevant operational indicators.
- Credential availability without exposing credential material.

Account detail should be organized into meaningful sections:

- Overview
- Plugins
- Profiles
- Runtime
- Channels
- Tasks
- Activity
- Account settings

Account operations include:

- Create account.
- Remove account.
- Start account.
- Stop account.
- Enable or disable account.
- Inspect account runtime state.
- Assign and unassign plugins.
- Manage account-specific settings.
- Inspect account-owned plugin profiles.

Destructive operations require confirmation and clear consequences.

Credential fields must be write-only. Never display stored tokens or return them to the UI.

## 5.3 Workspaces

Purpose: manage multi-account coordination.

A workspace is the coordination boundary for:

- Membership.
- Shared pacing clocks.
- Chains.
- Durable signals.
- Coordinated workflows.
- Workspace execution state.

The workspace interface should expose:

- Workspace identity.
- Member accounts.
- Membership changes.
- Workspace operational state.
- Shared coordination status.
- Chains and their definitions.
- Chain execution history.
- Signals and delivery status.
- Workspace presets.
- Execution checkpoints and recovery information.

Workspace details should provide distinct views for:

- Overview
- Members
- Chains
- Executions
- Signals
- Presets
- Activity

The UI must represent skipped chain steps and their reasons.

A skipped step must never be rendered as a successful execution.

Workspace membership must be manipulated through the API and its authoritative service. The UI must not independently maintain membership relationships.

## 5.4 Plugins

Purpose: provide visibility into available plugins and their account assignments.

The plugin catalog should display:

- Plugin name.
- Description.
- Version, if available.
- Discovery status.
- Manifest information.
- Available settings schema.
- Assignment coverage across accounts, where supported.

Plugin catalog management and account plugin configuration must remain separate.

**Plugin settings are account-bound.**

Each account independently owns its plugin assignment and configuration.

The UI must not introduce shared plugin configuration at the global or workspace level.

An account's plugin section is the authoritative location for configuring that plugin on that account.

Workspace-level plugin views may aggregate information for visibility, but configuration changes must use an explicit account context.

Bulk assignment or configuration operations must:

- Require explicit account targets.
- Report each account's result independently.
- Preserve successful operations when another account fails, where the backend contract allows partial success.
- Never imply that a failed account was updated.

## 5.5 Account plugin profiles

Purpose: manage reusable configuration instances belonging to an individual account and plugin.

The profile identity is:

`(account, plugin, profile_name)`

Each plugin owns its profile schema and validation rules.

The core stores and isolates profile data but must not interpret plugin-specific configuration fields.

The UI must:

- Display profiles in the correct account and plugin context.
- Support profile creation, selection, editing, and deletion through supported API operations.
- Respect plugin-defined defaults and validation.
- Show the currently selected profile.
- Distinguish profile configuration from runtime cooldown and execution state.
- Surface validation errors returned by the plugin or core.

Profiles must never be accidentally shared between accounts merely because their names match.

A profile named `default` under account A is independent from `default` under account B.

## 5.6 Workspace presets

Purpose: configure reusable multi-account workflows.

Workspace presets are not replacements for account plugin profiles.

They reference account-owned profiles using explicit identities.

The interface must distinguish:

- Profile configuration.
- Workspace preset definition.
- Workspace execution instance.
- Runtime workflow state.

Workspace presets coordinate execution and reference profiles. They do not become owners of plugin configuration.

The UI must show:

- Which accounts are referenced.
- Which plugin profiles are referenced.
- Whether referenced profiles still exist.
- Whether referenced profile revisions have changed.
- Workflow dependencies.
- Execution state and checkpoints.

A changed or missing profile must be reported visibly. Never silently substitute a different profile.

## 5.7 Tasks

Purpose: inspect and manage scheduled and executing work.

Expose:

- Task identifier.
- Account.
- Plugin.
- Task type.
- Current status.
- Priority.
- Creation and execution timestamps.
- Retry information.
- Failure reason.
- Cancellation availability.

Support relevant operations:

- Inspect task details.
- Cancel eligible tasks.
- Inspect task history.
- Filter by account, plugin, workspace, and status.

The UI must reflect the actual task transition rules.

Do not display actions that the backend will reject based on the current task state.

Do not invent task progress percentages when the scheduler does not expose them.

## 5.8 Rate Limits

Purpose: inspect and manage pacing policies.

Expose the supported policy scopes:

- Global.
- Account.
- Plugin.
- Account + plugin.

Display:

- Configured interval.
- Jitter.
- Effective interval.
- Learned fallback values, where applicable.
- Current safety floor.
- Active cooldowns.
- Server throttle information.
- Workspace shared-clock behavior.
- Relevant warnings.

Policy editing must use backend validation.

The UI must not permit values below the safety floor or imply that a local setting overrides server-provided limits.

Distinguish configured policy from effective policy.

## 5.9 Activity

Purpose: provide an observable history of application behavior.

Provide separate or clearly distinguishable views for:

- Runtime logs.
- Audit records.
- Task history.
- Workspace events.
- Signal delivery history.

Include useful filters:

- Account.
- Workspace.
- Plugin.
- Severity.
- Time range.
- Event category.

Sensitive values must be redacted.

Logs and audit records are read-only unless an explicit backend operation provides a supported management action.

## 5.10 Settings

Purpose: manage framework-level configuration.

Organize settings by their actual ownership and scope.

Possible sections:

- General application configuration.
- Runtime behavior.
- Global rate policies.
- Security and authorization.
- Appearance.
- Interface preferences.
- Diagnostics.

Do not introduce settings that have no corresponding backend contract.

Never use a UI preference to modify a runtime policy unless that preference explicitly represents that policy.

# 6. Interaction contract

Every interactive component must define its behavior before implementation.

For each operation, specify:

1. User intent.
2. Required context.
3. Input and validation.
4. Backend operation.
5. Loading behavior.
6. Success feedback.
7. Failure feedback.
8. State refresh behavior.
9. Cancellation or recovery behavior, where applicable.

## 6.1 Loading states

Use appropriate loading indicators.

Examples:

- Initial page load.
- Table refresh.
- Dialog data retrieval.
- Form submission.
- Account lifecycle operation.
- Workspace transition.
- Task cancellation.

Avoid freezing the entire interface during an operation that affects only one component.

## 6.2 Empty states

Empty states must explain what is missing and what the user can do next.

Examples:

- No accounts configured.
- No workspace members.
- No plugins discovered.
- No profiles created.
- No active tasks.
- No audit records.

Do not confuse an empty result with an API failure.

## 6.3 Error states

Errors must be understandable and actionable.

Distinguish:

- Validation errors.
- Authorization failures.
- Resource not found.
- Conflict or stale revision.
- Runtime operation failure.
- Network failure.
- API unavailable.
- WebSocket disconnection.

Preserve backend error meaning instead of replacing every error with a generic failure message.

## 6.4 Unknown state

Unknown is a legitimate state.

Missing data must not be converted into `false`, `true`, zero, or success without an explicit contract.

For example:

- Unknown connection status is not disconnected.
- Missing plugin runtime status is not enabled.
- Missing cooldown information is not necessarily no cooldown.
- Missing task data is not a completed task.

## 6.5 Destructive operations

Require clear confirmation for:

- Account removal.
- Workspace deletion.
- Plugin unassignment where it affects active behavior.
- Profile deletion.
- Preset deletion.
- Emergency stop.
- Other irreversible or operationally disruptive actions.

Confirmation must describe the actual consequence, not merely say "Are you sure?"

Emergency stop must remain visually distinct and must not be placed beside ordinary navigation actions without appropriate separation.

# 7. Visual design system

## 7.1 Design direction

Build a coherent desktop-oriented operational application.

The interface should feel like a professional control panel for managing a complex runtime, not a generic analytics dashboard.

Prioritize:

- Clear information hierarchy.
- Dense but readable operational data.
- Consistent spacing.
- Predictable navigation.
- Meaningful status visualization.
- Minimal decorative elements.
- Clear separation between configuration and runtime state.

Do not sacrifice usability for visual effects.

## 7.2 Theme system

Implement a centralized theme system.

At minimum support:

- Dark theme.
- Light theme.
- Persistent user preference.

Use centralized design tokens for:

- Backgrounds.
- Surface levels.
- Text colors.
- Borders.
- Accent colors.
- Status colors.
- Spacing.
- Corner radii.
- Typography.
- Control dimensions.

Do not scatter hard-coded colors throughout page modules.

All shared components must respond to theme changes.

## 7.3 Status semantics

Use consistent visual semantics:

- Success: completed or healthy.
- Warning: degraded or requires attention.
- Error: failed or unavailable.
- Neutral: inactive or informational.
- Unknown: state cannot currently be determined.

Color must not be the only way to communicate status. Include readable text or an accessible label.

## 7.4 Responsive behavior

The primary target is a desktop application, but the layout must adapt to narrower windows.

Support:

- Expanded navigation on wide windows.
- Compact or collapsed navigation on narrow windows.
- Tables that remain usable at reduced widths.
- Dialogs that fit the available viewport.
- Scrollable content without clipping important actions.
- Appropriate control sizes and spacing.

Do not assume a fixed screen resolution.

# 8. State management and live updates

## 8.1 State ownership

The backend owns authoritative application state.

The Flet interface may maintain:

- Current navigation route.
- Selected management context.
- Temporary form values.
- Local filters.
- Expanded and collapsed sections.
- Presentation preferences.
- Cached read-only snapshots.

It must not become the authority for account, plugin, workspace, task, or scheduler state.

## 8.2 API client

The UI API client must:

- Use the configured API base URL.
- Support authentication.
- Use asynchronous HTTP operations.
- Set bounded connection and request timeouts.
- Serialize and deserialize JSON consistently.
- Normalize HTTP errors.
- Handle connection failures.
- Support cancellation where practical.
- Close resources during application shutdown.

Prefer the already declared `httpx` dependency rather than introducing an unnecessary second HTTP client.

Do not perform blocking network requests inside Flet event callbacks.

## 8.3 WebSocket integration

The UI should subscribe to relevant application events.

Expected behavior:

- Initial state is loaded through REST.
- Live events update the relevant presentation state.
- Reconnection is automatic and bounded.
- Disconnection is visible.
- Reconnection triggers reconciliation with an authoritative REST snapshot.
- Duplicate events do not produce duplicate UI actions.
- Old subscriptions are closed when the relevant page or session is disposed.

Do not use periodic full-page refreshes as a substitute for event-driven updates.

The UI must remain usable when the WebSocket is disconnected.

## 8.4 Stale data

Indicate when displayed information may be stale.

Do not present an old cached snapshot as current runtime truth after a connection failure.

Mutations must be confirmed by backend responses. Optimistic presentation is permitted only where rollback and reconciliation are well-defined.

# 9. Security requirements

- API credentials must never be hard-coded.
- Tokens must never be displayed in ordinary UI components.
- Credential inputs must be write-only.
- Never log authorization headers.
- Authenticate REST and WebSocket access.
- Validate API responses before using them in sensitive presentation contexts.
- Preserve exact Discord snowflake identifiers as strings.
- Never convert snowflakes into floating-point or imprecise numeric representations.
- Do not expose internal exception tracebacks to ordinary users.
- Do not allow UI-level operations to bypass core authorization.
- Destructive actions must be explicit.
- Avoid leaking sensitive values in notifications, error messages, or diagnostic views.

# 10. Runtime and application lifecycle

The interface must integrate with the existing composition root.

`core/bootstrap.py::build()` remains the application runtime construction boundary.

The UI must not independently construct another runtime just to serve a page.

Before implementation, explicitly settle and document the supported launch model:

- Standalone Flet application connecting to the local FastAPI service.
- Embedded Flet application with FastAPI hosted in the same application process.
- Or both, with one documented production mode and a supported development mode.

Regardless of the chosen mode:

- There must be one authoritative runtime.
- Startup ordering must be deterministic.
- Shutdown must close the UI's API and WebSocket resources.
- The framework must shut down cleanly.
- UI failure must not silently leave background resources running.
- No duplicate application bootstrap may occur.

The chosen model must work with the existing CLI and headless modes.

# 11. Dependency and compatibility requirements

The project currently declares FastAPI and httpx, while Flet is imported lazily and is not present in the shown runtime dependency list.

Resolve this deliberately.

If Flet is mandatory for the supported UI installation, declare and lock it appropriately.

If Flet is an optional installation extra, document how to install and launch it, and ensure the CLI and API can operate without it.

Do not leave the application in a state where the UI is documented as supported but cannot be installed from the declared dependency configuration.

Preserve the project's supported Python version unless a deliberate compatibility decision changes it.

Verify the Flet API against the installed, pinned version. Do not rely on examples from an unrelated Flet release.

# 12. Testing and acceptance requirements

The interface is not complete merely because the application opens.

Test the following.

## 12.1 Shell

- Application starts.
- All registered pages appear.
- Navigation works.
- Active navigation state is correct.
- Invalid page identifiers fail safely.
- Page changes do not leak subscriptions.
- Theme changes are applied consistently.

## 12.2 API integration

- Authenticated requests succeed.
- Missing or invalid tokens are rejected.
- Network failures are surfaced.
- Invalid responses do not corrupt UI state.
- Timeouts are handled.
- REST and WebSocket authentication are consistent.
- API operations use the shared ControlAPI.

## 12.3 Functional pages

Test all relevant CRUD and management operations for:

- Accounts.
- Workspaces.
- Plugins.
- Account plugin profiles.
- Workspace presets.
- Tasks.
- Rate limits.
- Logs and audit records.
- Settings.

Verify that the displayed result matches persisted backend state.

## 12.4 Isolation

Test that:

- Identically named profiles under different accounts remain independent.
- Plugin settings are never accidentally shared between accounts.
- Workspace presets reference the correct account/plugin/profile identities.
- Changing workspace context does not silently change operation targets.
- One account's failure does not falsely report success for other accounts.
- Unknown runtime data remains unknown.

## 12.5 Failure and recovery

Test:

- API server unavailable at startup.
- API server disconnects during use.
- WebSocket disconnects and reconnects.
- A mutation fails.
- A mutation succeeds but the UI refresh fails.
- Stale revision conflict.
- Resource deleted while its page is open.
- User navigates away during an operation.
- Application closes during a pending request.

## 12.6 Accessibility and usability

Verify:

- Keyboard navigation.
- Focus visibility.
- Dialog focus handling.
- Accessible names for icon-only controls.
- Readable status information.
- Text scaling where supported.
- Narrow-window behavior.
- Empty, loading, error, and success states.

# 13. Implementation phases

Implement incrementally.

### Phase 1 — Interface foundation

- Confirm the Flet dependency and supported version.
- Confirm the launch and lifecycle model.
- Establish the API client.
- Establish the application shell.
- Establish navigation and page registration.
- Establish centralized themes and design tokens.
- Establish session and subscription ownership.

### Phase 2 — Operational foundation

Implement:

- Overview.
- Accounts list and account detail.
- Workspaces list and workspace detail.
- Tasks.
- Activity.

These pages establish the primary operational experience.

### Phase 3 — Configuration

Implement:

- Plugin catalog.
- Account plugin assignments.
- Account plugin profiles.
- Rate-limit policies.
- Framework settings.

### Phase 4 — Workspace coordination

Implement:

- Workspace chains.
- Chain execution inspection.
- Signals and delivery history.
- Workspace presets.
- Execution checkpoints and recovery visibility.

### Phase 5 — Live integration

- Actual event-driven WebSocket integration.
- Reconnection and snapshot reconciliation.
- Live runtime updates.
- Notifications.
- Cross-page state consistency.

### Phase 6 — Acceptance and stabilization

- Functional tests.
- Isolation tests.
- Failure and recovery tests.
- Accessibility checks.
- Responsive checks.
- Theme verification.
- Lifecycle and resource-leak tests.
- Documentation and installation updates.

Do not begin implementing every page simultaneously. Complete the shared foundation before expanding the page surface.

# 14. Deliverables

The implementation must provide:

1. A documented Flet application architecture.
2. A reusable shell and navigation system.
3. A centralized theme system.
4. A maintainable component library.
5. A reusable asynchronous API client.
6. A consistent page implementation pattern.
7. Functional operational pages.
8. Live update integration.
9. Error, loading, empty, and unknown states.
10. Automated UI and API integration tests.
11. Installation and launch instructions.
12. Updated documentation reflecting the actual supported interface.

# 15. Definition of done

The interface is accepted only when:

- Flet is the actual UI framework, not a wrapper around the retired SPA.
- All interface operations use the shared API boundary.
- The core remains the authority for application behavior.
- Account and plugin data isolation is preserved.
- Workspace coordination is represented correctly.
- The UI distinguishes configuration from runtime state.
- Every mutation has meaningful feedback.
- Errors and unknown states are handled honestly.
- The API client does not block the Flet event loop.
- Live updates work and recover from disconnection.
- The interface works across supported window sizes and themes.
- Tests verify observable behavior and backend persistence.
- Startup and shutdown do not leak resources.
- Documentation and dependency declarations match the implementation.

**Final architectural principle:**

LibzentSuite has one application core, one shared ControlAPI, and multiple interfaces.

The Flet UI is a client of that application—not a second application, not a second orchestration engine, and not an alternate source of truth.

Design the interface around the actual capabilities and ownership boundaries of LibzentSuite.
