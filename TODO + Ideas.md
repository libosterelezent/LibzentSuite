Discord Automation Framework — Project Ideas & TODO

ARCHITECTURE

The project can be structured as a multi-account automation framework with:
- Modular plugins
- Centralized account management
- Centralized settings
- Centralized rate limiting
- Profiles/capabilities
- Database persistence
- CLI and GUI dashboard
- Centralized logging
- Simulation/testing system

Suggested architecture:

                    CLI / GUI
                       |
                       v
                 Core Manager
                       |
        +--------------+--------------+
        |              |              |
 Account Manager  Plugin Manager  Settings Manager
        |              |              |
        +--------------+--------------+
                       |
                Task Scheduler
                       |
                Rate Limit Manager
                       |
                 Discord Adapter
                       |
                    Database


IMPORTANT DESIGN PRINCIPLE

Plugins should not directly manage:
- Accounts
- Database
- GUI state
- CLI state
- Global rate limiting

Plugins should request services from the core framework instead.

This keeps plugins independent and makes future plugins much easier to create.


1. ACCOUNT MANAGER

Make Account a first-class object instead of using scattered dictionaries.

Account:
- id
- name
- client
- status
- enabled
- settings
- assigned profile/capabilities
- loaded plugins
- runtime state
- rate-limit policy

Separate persistent data from runtime data.

Persistent:
- Account ID
- Account name
- Enabled state
- Settings
- Assigned profiles
- Plugin settings
- Rate-limit policy

Runtime:
- Client instance
- Connection state
- Cached guild/user/channel information
- Latency
- Ready state
- Runtime statistics

Account details should display only information already available from the running client's cached/runtime state when appropriate.


2. PLUGIN ARCHITECTURE

Plugins should be self-describing.

Plugin:
- Metadata
- Version
- Description
- Settings schema
- Lifecycle hooks
- Commands/actions
- Runtime state

Example settings:

    enabled
    cooldown
    option_a
    option_b

The framework should be able to automatically generate settings interfaces from the plugin's settings schema instead of hardcoding every plugin's GUI.


3. SHARED SETTINGS SYSTEM

Every feature with settings should be accessible through both:
- CLI
- Dashboard

Do NOT create separate settings implementations for CLI and GUI.

Instead:

             Settings Schema
              /                        /                        CLI             GUI

Example:

    Setting(
        name="cooldown",
        type=float,
        default=2.0,
        minimum=1.5
    )

The CLI and GUI both consume the same schema.


4. PROFILES / CAPABILITIES  — SUPERSEDED (2026-10-22)

The original "Role"/Profile concept (Roller/Sniper/Collector bundles) was
built as ProfileManager and FAILED — it gated nothing and its bookkeeping
outlived its purpose (full removal plan: consult-B1-profilemanager.md).

What replaced it:
- Capabilities are DIRECT account -> plugin assignments (account_plugins)
- Named settings presets live as per-plugin profiles (plugin_profiles,
  ALIVE — bound per account from the account MEMORY view)
- Per-channel operational data is the AccountPluginData store
  (plugin-data.db — see docs/design/fleet-orchestration-hub.md §7)

Do not reintroduce permission bundles. Accounts, plugins, and scopes are
the axes; profiles were the wrong abstraction.


5. RATE LIMIT MANAGER

Use a centralized scheduler/rate-limit manager.

Possible policy levels:
- Global
- Account
- Plugin
- Account + Plugin

Example:

    Global:
        minimum interval = 2.0 sec

    Account A:
        inherits global

    Account B:
        minimum interval = 3.0 sec

    Account C:
        minimum interval = 2.5 sec

Useful rate-control concepts:
- Minimum interval
- Random jitter
- Priority
- Concurrency
- Backoff
- Server-provided rate-limit handling

The randomized delay should be treated as request pacing/load management, not as a method for disguising automation or bypassing service enforcement.

Suggested UI:

    Base interval: 2.0 sec
    Random jitter: 0.0 - 0.5 sec

The framework should always respect server-provided rate-limit information.


6. TASK SCHEDULER

Add a centralized task scheduler.

Responsibilities:
- Queueing
- Priorities
- Cancellation
- Concurrency
- Rate limiting
- Retries
- Timeouts
- Graceful shutdown

Concept:

    Plugin
       |
       v
    Task Scheduler
       |
       +-- Priority
       +-- Rate Limit
       +-- Concurrency
       +-- Cancellation
       |
       v
    Discord Adapter


7. DATABASE

Use a proper database instead of storing everything in one large JSON file.

    libzent.db (SQLite, versioned migrations):
    accounts, account_settings, plugins, plugin_settings,
    plugin_profiles, rate_limit_policies, audit_logs, tasks

    plugin-data.db (SQLite, self-initializing — PHASE 9/B3):
    plugin_data(account, plugin, guild_id, channel_id, data_json, ...)

Secrets should not be treated as automatically secure simply because they are stored in SQLite. Use an OS credential/keyring mechanism where appropriate.


8. ACCOUNT DETAILS PAGE

Example:

    Account: Example

    Status
    ----------------
    Connected
    Latency: 83 ms
    Uptime: 2h 14m

    Identity
    ----------------
    Name
    ID

    Runtime
    ----------------
    Cached guilds
    Cached users
    Cached channels

    Plugins
    ----------------
    Roll       Enabled
    Snipe      Disabled

    Rate Limit
    ----------------
    Inherited from global

The page should clearly indicate when information comes from the current client's runtime/cache.


9. SIMULATION SYSTEM

Create a Discord client abstraction so the framework can use either a real adapter or a simulation adapter.

Concept:

    DiscordClient Interface
          /                   /                Real Client     Simulator

The simulator can generate events such as:

    MESSAGE_RECEIVED
    COMMAND
    REACTION
    CHANNEL_UPDATE
    RATE_LIMIT
    ERROR
    DISCONNECT

Example simulation log:

    00:00.000  Plugin loaded
    00:00.012  Task created
    00:02.017  Action scheduled
    00:02.021  Action executed
    00:02.025  Response simulated
    00:04.500  Cooldown completed

This allows plugin behavior and scheduler logic to be tested without repeatedly interacting with the real service.


10. CENTRALIZED LOGGING

Create one logging service for:

    Core
    Account
    Plugin
    Scheduler
    RateLimiter
    Discord Adapter

Each event should contain:

    timestamp
    account_id
    plugin
    task_id
    event
    severity
    message

Example:

    21:04:12.221
    Account=AccountA
    Plugin=Roll
    Task=1842
    INFO
    Action scheduled

The dashboard can then display live logs without parsing text files.


11. EVENT BUS

Add a centralized Event Bus.

Possible events:

    Account connected
    Account disconnected
    Plugin loaded
    Plugin unloaded
    Task queued
    Task started
    Task completed
    Rate limit triggered
    Setting changed

Concept:

    Account Manager
          |
          v
       Event Bus
       /   |         /    |     Dashboard Logger CLI


12. DASHBOARD STRUCTURE

Suggested navigation:

    Dashboard
    |
    +-- Overview
    |
    +-- Accounts
    |    +-- Account list
    |    +-- Account details
    |
    +-- Plugins
    |    +-- Installed plugins
    |    +-- Plugin settings
    |
    +-- Profiles
    |    +-- Profiles
    |    +-- Profile assignments
    |
    +-- Rate Limits
    |    +-- Global
    |    +-- Per-account
    |
    +-- Tasks
    |    +-- Running
    |    +-- Queued
    |    +-- History
    |
    +-- Logs
    |    +-- Live logs
    |
    +-- Simulation
    |    +-- Scenarios
    |    +-- Run
    |    +-- Results
    |
    +-- Advanced
         +-- Database
         +-- Logging
         +-- Runtime


13. CLI STRUCTURE

Suggested commands:

    app accounts list
    app accounts add
    app accounts remove
    app accounts info <account>

    app plugins list
    app plugins enable <plugin>
    app plugins disable <plugin>
    app plugins settings <plugin>

    app ratelimit global
    app ratelimit account <account>

    app tasks list
    app tasks cancel <task>

    app simulation run
    app logs live

CLI and GUI should call the same Application/Core API.


RECOMMENDED PROJECT STRUCTURE

    project/
    |
    +-- core/
    |    +-- account_manager
    |    +-- plugin_manager
    |    +-- settings_manager
    |    +-- rate_limit_manager
    |    +-- sync_group_manager
    |    +-- task_scheduler
    |    +-- event_bus
    |    +-- logging
    |
    +-- database/
    |    +-- models
    |    +-- repositories
    |    +-- migrations
    |
    +-- plugins/
    |    +-- plugin_a
    |    +-- plugin_b
    |    +-- ...
    |
    +-- adapters/
    |    +-- discord
    |    +-- simulator
    |
    +-- interfaces/
    |    +-- cli
    |    +-- dashboard
    |
    +-- tests/
    |    +-- unit
    |    +-- integration
    |    +-- simulation
    |
    +-- config/
    |
    +-- main.py


TODO LIST

PHASE 1 — CORE  ✅ DONE

[x] Account manager
[x] Plugin manager
[x] Settings system
[x] Event bus
[x] Task scheduler
[x] Centralized logging
[x] Database layer


PHASE 2 — ACCOUNT MANAGEMENT  ✅ DONE

[x] Add account
[x] Remove account
[x] Account list
[x] Account runtime state
[x] Account details
[x] Enable/disable account


PHASE 3 — PLUGIN SYSTEM  ✅ DONE

[x] Plugin discovery
[x] Plugin metadata
[x] Plugin settings schema
[x] Plugin lifecycle
[x] Enable/disable plugins
[x] Plugin settings API


PHASE 4 — PROFILES / CAPABILITIES  ✅ DONE

[x] Create profile
[x] Assign plugins to profile
[x] Assign profile to account
[x] Profile settings
[x] Multiple accounts sharing profile


PHASE 5 — RATE MANAGEMENT  ✅ DONE

[x] Global policy
[x] Account policy
[x] Plugin policy
[x] Randomized pacing/jitter
[x] Server-provided rate-limit handling
[x] Configuration validation
[x] Dangerous-setting warnings
[x] Rate-limit visualization


PHASE 6 — INTERFACES  ✅ DONE

[x] CLI API
[x] Dashboard API
[x] Account pages
[x] Plugin pages
[x] Profile pages
[x] Rate-limit pages
[x] Live logs


PHASE 7 — SIMULATION  ✅ DONE

[x] Mock Discord adapter
[x] Event generator
[x] Scenario system
[x] Simulation runner
[x] Detailed execution logs
[x] Regression tests


PHASE 8 — SAFETY / VALIDATION  ✅ DONE

[x] Dangerous-setting warnings
[x] Configuration validation
[x] Minimum pacing constraints
[x] Global emergency stop
[x] Account emergency stop
[x] Plugin emergency stop
[x] Graceful shutdown


PHASE 6b — DASHBOARD UI v3 REBUILD  ✅ DONE

[x] Hash-routed SPA (Carbon design system, tokens + canonical classes)
[x] Bespoke account-as-object view (hero / abilities / memory / pacing / live stats)
[x] Generic builder-tree renderer for plugin pages (render.js)
[x] Theme engine (presets, accent, radius, density, fonts, custom CSS)
[x] Contract tests pinning endpoints, classes, tokens (test_dashboard_static.py)

Architecture decision (locked): plugins build UI through the SAFE BUILDER
(core/dashboard_ui -> render.js); the framework's own flagship views stay
BESPOKE (js/views/*). Modular split — the design system (tokens + canonical
classes) is the single shared contract; static tests are the drift police.
Decision rule: could a plugin own the surface? -> builder. Needs routing
params / selection state / WS-diffed live data / multi-step flows? -> bespoke.


PHASE 9 — GROUP RELAY: CENTRALIZED ACTION CONTROL  (planned — DESIGN LOCKED)

The entire point of Groups: control the actions of MULTIPLE accounts
CENTRALLY — not merely share a pacing clock.

DESIGN SPEC: docs/design/fleet-orchestration-hub.md (decisions locked
2026-10-22: scheduling ownership + INDEPENDENT|GROUP_MANAGED mode flag,
db-authoritative state, operator-assign + strategy slot with relay as
strategy #1, drain-default transitions, channel-scoped tasks day one,
dedicated Fleet Hub route). Consult sources: GPT (repo-connected) +
Gemini plans in state/ui-rebuild-2026-10-22/.

[x] Group CRUD + membership (exists — SyncGroupManager)
[x] Grouped accounts serialize on the shared limiter clock (exists)

[ ] RELAY / TURN-TAKING (the real flow):
      A rolls -> Mudae limit message lands on A
      -> A marked SPENT for the window
      -> B becomes ACTIVE and starts rolling
      -> (all members spent) -> group cooldown -> reset -> A active again
[ ] RotationController: thin coordinator ABOVE SyncGroupManager — subscribes
    to parser events, owns queue state (ACTIVE / WAITING / SPENT / COOLING),
    gates which account may act. Modularity rule: the limiter stays pure
    pacing (safety floor); the relay decides WHO may go, the limiter still
    decides HOW FAST. One being wrong must never violate the other.
[ ] Handoff sensor: MudaeParser rule family for per-locale limit markers
    (roulette / roll variants)
[ ] Reset semantics: event-based (Mudae's own reset message) preferred,
    window-based fallback
[ ] PACING BETWEEN GROUPED ACCOUNTS: shared-clock middle pacing is NOT
    necessarily desirable under relay — but it stays EDITABLE, never removed
    (per-group pacing mode: shared-clock | relay | off; floor always applies)
[ ] Stats frame carries per-group rotation state (whose turn, spent-since,
    resets-in) so the dashboard shows the flow live
[ ] Group detail view in the dashboard: member queue with live turn state,
    handoff history
[ ] Account hero shows group turn state (e.g. "grp farm-a · waiting, turn: B")
[ ] Room for more group strategies later — the coordinator is a strategy
    slot, relay is only the first strategy


PHASE 10 — PLUGIN METRICS & FLOWS  (planned)

[ ] Plugins supply metrics INTO the dashboard: ui_stats StatCards already
    surface per-account in the account detail LIVE STATS section — keep
    growing what plugins report (roll counts, claims, hits, streaks)
[ ] Plugin-authored FLOWS: the operator will build real behaviors inside
    plugins (rolling, claiming, reactions) that emit events + telemetry
    through the event bus — the dashboard reads, plugins speak
[ ] Plugin flows must ride the SAME paced pipeline (ctx.services), never
    raw clients — the plugin contract holds as flows grow
[ ] Metrics feed the group relay: per-account plugin counters make the
    rotation numerically visible (A: 8 rolls this window, B: 0)


DEPRECATED CORE PARTS — REMOVED 2026-10-22

ProfileManager (core/profile_manager/) — the named permission-bundle
system. It gated nothing (capabilities are direct account->plugin
assignments) and was removed completely: package, ControlAPI
profiles_* facade, /api/profiles* routes, CLI profiles group,
AccountsRepository assignment methods, ProfileRecord, and migration 8
dropping the profiles / profile_plugins / account_profiles tables.
Superseded by: account_plugins (capabilities), plugin_profiles (v7
settings presets), and plugin-data.db (per-channel operational data).
See section 4 above and docs/design/fleet-orchestration-hub.md.


ACTIVE QUEUE  (2026-10-23, ordered)

[x] PHASE A-11 refactor queue — DONE (2026-10-23). Tabbed ability
    dialog, state-kv strip, theme Layout tab, contract tests, AND the
    channel-scope editor: positive channel-set (plugin_scopes) with a
    dropdown channel-tree picker in the profiles editor. Live-verified
    on bravo: 6 real guilds render, selection stores the positive set,
    library/tiles reflect it. Also fixed in the pass: builder
    tile-view renderer, records-shape tables, dynamic-field refresh in
    modals (commits 7db9752, 2542822).
[x] Legacy permission-bundle manager REMOVAL — DONE (PR #16 merged
    into feat/ui-rebuild).
[ ] PHASE C LIVE DEBUG — resumed 2026-10-23: bravo re-credentialed
    (new token) and RUNNING; profiles editor round-trip verified live.
    Blocked: alpha still needs a fresh credential. Remaining:
    admin-command flow B -> A, ordered full walkthrough.
[ ] PHASE 9 ROLLOUT — design locked (docs/design/
    fleet-orchestration-hub.md). Progress:
    A. DONE (2026-10-23, d640cec): core/fleet/ models + transition
       tables (46 pure tests) — member eligibility, assignment states
       with leave/retry guards, scoped cooldown records (most-
       restrictive-wins), task definitions (channel scope, field
       classes, metric hints), generation fence, event envelope.
    B. DONE (2026-10-23, f1e99d8): FleetService — the ONE writer of
       the fleet projection (join/leave, pauses, full assignment
       lifecycle, handoff with destination-first validation, cooldown
       apply/clear), migration v9 + FleetRepository, write-ahead
       history + event seam, generation bumping; 33 service tests on
       real in-memory SQLite (79 fleet tests total).
    C-G pending. NOTE: tests/unit/test_task_scheduler has ~7
    pre-existing timing flakes on HEAD (completed 1!=2 family) —
    triage before Phase C leans on that suite.
[ ] PHASE 10 ROLLOUT — after PHASE 9 signals exist.


ORIGINAL TODO ITEMS TO PRESERVE — all shipped (2026-10-22); the section is
retired. History lives in git.

CORE DESIGN GOAL

The most important architectural goal is:

    One Core API
          |
      +---+---+
      |       |
     CLI     GUI

Both interfaces should manipulate the exact same underlying:
- Accounts
- Plugins
- Settings profiles
- Tasks
- Rate-limit policies
- Logs
- Simulation system

Plugins should remain independent from the interface layer.
