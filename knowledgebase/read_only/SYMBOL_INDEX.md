# SYMBOL INDEX (generated, READ ONLY)

> Commit `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1` — regenerate with `python3 scripts/gen_knowledgebase.py`. Machine-readable form: `knowledgebase/generated/symbol_index.json`.

| Module | Classes | Functions | Symbols | Purpose |
|---|---|---|---|---|
| [`adapters`](modules/adapters.md) | 0 | 0 | 0 | Adapters: the Discord boundary of the framework. |
| [`adapters.base`](modules/adapters.base.md) | 2 | 0 | 10 | Discord client abstraction (spec §9): ONE interface, two implementations. |
| [`adapters.discord`](modules/adapters.discord.md) | 0 | 0 | 0 | Real Discord adapter: the live-client half of the adapter boundary. |
| [`adapters.discord.client`](modules/adapters.discord.client.md) | 3 | 5 | 61 | Real Discord client adapter (ported from the audited core/discord_adapter.py). |
| [`adapters.model`](modules/adapters.model.md) | 9 | 11 | 20 | Rich project-owned Discord message/event model (the adapter boundary). |
| [`adapters.simulator`](modules/adapters.simulator.md) | 0 | 0 | 0 | Simulator adapter: the offline half of the Discord boundary. |
| [`adapters.simulator.client`](modules/adapters.simulator.client.md) | 1 | 0 | 22 | SimClient — the simulated half of the adapter boundary (ported from the |
| [`adapters.simulator.runner`](modules/adapters.simulator.runner.md) | 1 | 4 | 9 | SimulationRunner — a COMPLETE ephemeral stack per scenario run (spec §16). |
| [`adapters.simulator.scenarios`](modules/adapters.simulator.scenarios.md) | 2 | 19 | 19 | Scenario definitions for the offline simulation suite (spec §16). |
| [`adapters.simulator.world`](modules/adapters.simulator.world.md) | 3 | 2 | 24 | SimWorld — a complete in-memory Discord world (ported from the audited |
| [`core`](modules/core.md) | 0 | 0 | 0 | LibzentSuite core framework. |
| [`core._asyncutil`](modules/core._asyncutil.md) | 0 | 2 | 2 | Small asyncio helpers shared by the event bus and the task scheduler. |
| [`core._persist_queue`](modules/core._persist_queue.md) | 1 | 0 | 14 | Shared queued-persistence engine for the log and audit sinks. |
| [`core.access_manager`](modules/core.access_manager.md) | 1 | 1 | 29 | AccessManager — the DB-backed authorization/prefix/admin store. |
| [`core.account_manager`](modules/core.account_manager.md) | 0 | 0 | 0 | Account manager package (A6): Account (first-class), AccountManager, DiscordCache. |
| [`core.account_manager.cache`](modules/core.account_manager.cache.md) | 2 | 2 | 15 | Per-account Discord cache: guilds, channels, DM channels — real-time first. |
| [`core.account_manager.lifecycle`](modules/core.account_manager.lifecycle.md) | 6 | 0 | 13 | Account lifecycle rate limiting — start/stop/restart abuse prevention. |
| [`core.account_manager.manager`](modules/core.account_manager.manager.md) | 5 | 2 | 70 | Account manager (A6): Account, AccountRuntime, AccountManager, DiscordCache wiring. |
| [`core.account_manager.services`](modules/core.account_manager.services.md) | 3 | 0 | 44 | PluginServices implementation for the account manager (A6). |
| [`core.api`](modules/core.api.md) | 1 | 2 | 117 | CoreAPI — the ONE application surface both interfaces call (spec §13/§14). |
| [`core.audit`](modules/core.audit.md) | 2 | 3 | 13 | Interaction audit trail: every Discord touch the framework makes. |
| [`core.bootstrap`](modules/core.bootstrap.md) | 1 | 7 | 12 | Composition root: builds the ENTIRE framework from config + credentials. |
| [`core.config`](modules/core.config.md) | 1 | 4 | 4 | Project configuration loading (config/ directory, all optional). |
| [`core.cooldowns`](modules/core.cooldowns.md) | 1 | 0 | 12 | CooldownService — generic persistent cooldowns with restart recovery. |
| [`core.credential_service`](modules/core.credential_service.md) | 1 | 0 | 14 | CredentialService — encrypted credential storage (spec §12-§14). |
| [`core.credentials`](modules/core.credentials.md) | 1 | 0 | 16 | Credential handling: every token and user/bot ID lives in .env, never in code. |
| [`core.dashboard_ui`](modules/core.dashboard_ui.md) | 0 | 0 | 0 | Dashboard UI API — builder core (Stage B) + registry (Stage C). |
| [`core.dashboard_ui._base`](modules/core.dashboard_ui._base.md) | 5 | 9 | 65 | Dashboard UI builder primitives. |
| [`core.dashboard_ui.builders`](modules/core.dashboard_ui.builders.md) | 2 | 0 | 6 | Dashboard UI builder core: fluent, validated, JSON-serializable components. |
| [`core.dashboard_ui.components`](modules/core.dashboard_ui.components.md) | 36 | 0 | 151 | The dashboard UI component catalog. |
| [`core.dashboard_ui.context`](modules/core.dashboard_ui.context.md) | 3 | 2 | 2 | Callback context types for the dashboard UI API (Stage C). |
| [`core.dashboard_ui.errors`](modules/core.dashboard_ui.errors.md) | 11 | 0 | 1 | Dashboard UI error hierarchy. |
| [`core.dashboard_ui.registry`](modules/core.dashboard_ui.registry.md) | 1 | 0 | 23 | DashboardUIRegistry — the central registry for plugin dashboard UIs. |
| [`core.errors`](modules/core.errors.md) | 17 | 1 | 4 | Domain errors for the whole framework. |
| [`core.event_bus`](modules/core.event_bus.md) | 22 | 0 | 12 | Event bus + the framework's typed event vocabulary (spec §11). |
| [`core.framework_ui`](modules/core.framework_ui.md) | 1 | 25 | 30 | The framework's OWN dashboard pages, built with the plugin UI API. |
| [`core.framework_ui_design`](modules/core.framework_ui_design.md) | 0 | 3 | 3 | Design gallery — the framework UI's component sandbox page. |
| [`core.logging_service`](modules/core.logging_service.md) | 4 | 8 | 35 | Centralized logging service (spec §10). |
| [`core.names`](modules/core.names.md) | 0 | 0 | 0 | The three namespace validators (accounts, profiles, plugin slugs). |
| [`core.plugin_base`](modules/core.plugin_base.md) | 10 | 3 | 43 | Plugin contract: base class, runtime context, services protocol (spec §4-6). |
| [`core.plugin_manager`](modules/core.plugin_manager.md) | 0 | 0 | 0 | Plugin Manager (spec §2): discovery, per-account instances, dispatch. |
| [`core.plugin_manager.commands`](modules/core.plugin_manager.commands.md) | 3 | 22 | 38 | Self-command hub: the per-account command registry (Phase 3). |
| [`core.plugin_manager.loader`](modules/core.plugin_manager.loader.md) | 2 | 0 | 11 | Plugin discovery: finds PluginBase subclasses under a plugin directory. |
| [`core.plugin_manager.manager`](modules/core.plugin_manager.manager.md) | 3 | 0 | 64 | PluginManager: per-account plugin instances, lifecycle, dispatch (spec §2). |
| [`core.plugin_scopes`](modules/core.plugin_scopes.md) | 0 | 3 | 3 | Reserved settings structure: channel scopes (single source of truth). |
| [`core.plugin_state`](modules/core.plugin_state.md) | 1 | 0 | 21 | PluginStateService — generic scoped persistence for plugins (spec §20). |
| [`core.priority`](modules/core.priority.md) | 1 | 0 | 1 | Shared task/priority ordering used by BOTH the rate limiter and scheduler. |
| [`core.rate_limit_manager`](modules/core.rate_limit_manager.md) | 0 | 0 | 0 | Rate-limit manager package (A4): 4-level policies, pacing engine, |
| [`core.rate_limit_manager.engine`](modules/core.rate_limit_manager.engine.md) | 6 | 0 | 47 | Pacing engine: priority waiters, server cooldowns, jitter (A4). |
| [`core.rate_limit_manager.lane`](modules/core.rate_limit_manager.lane.md) | 2 | 0 | 14 | GlobalSendLane — FarmLite's sequential-send concept, made robust (spec add-on). |
| [`core.rate_limit_manager.policies`](modules/core.rate_limit_manager.policies.md) | 1 | 2 | 4 | Rate policies: the 4-level policy model + validation (spec §5, A4). |
| [`core.rate_limit_manager.warnings`](modules/core.rate_limit_manager.warnings.md) | 0 | 5 | 5 | Safety warnings for the rate-limit configuration (A4). |
| [`core.remote_control`](modules/core.remote_control.md) | 1 | 0 | 18 | Remote control: manage the framework FROM Discord via mention prefixes. |
| [`core.settings_manager`](modules/core.settings_manager.md) | 2 | 1 | 13 | Shared settings system (spec §3): ONE schema, consumed by CLI + GUI alike. |
| [`core.sync_group_manager`](modules/core.sync_group_manager.md) | 0 | 0 | 0 | Sync group manager package: first-class groups + membership ownership. |
| [`core.sync_group_manager.manager`](modules/core.sync_group_manager.manager.md) | 1 | 0 | 25 | SyncGroupManager — the application-level owner of sync group membership. |
| [`core.task_scheduler`](modules/core.task_scheduler.md) | 0 | 0 | 0 | Centralized task scheduler package (A5). |
| [`core.task_scheduler.scheduler`](modules/core.task_scheduler.scheduler.md) | 3 | 0 | 43 | TaskScheduler — centralized queue, per-account lanes, retry/timeout/cancel. |
| [`core.task_scheduler.tasks`](modules/core.task_scheduler.tasks.md) | 1 | 0 | 2 | Task model for the centralized scheduler (spec §6, INTEGRATION 'core/task_scheduler'). |
| [`core.theme_store`](modules/core.theme_store.md) | 0 | 2 | 2 | Theme store — the dashboard theme engine's durable state. |
| [`database`](modules/database.md) | 0 | 0 | 0 | Persistence layer: SQLite models, schema migrations, repositories. |
| [`database.connection`](modules/database.connection.md) | 2 | 1 | 17 | Async database connection layer (aiosqlite) — single long-lived connection. |
| [`database.db`](modules/database.db.md) | 1 | 0 | 6 | Async SQLite persistence service (aiosqlite) — the app's single DB owner. |
| [`database.models`](modules/database.models.md) | 5 | 0 | 6 | Database models: plain dataclasses crossing the persistence boundary. |
| [`database.repositories`](modules/database.repositories.md) | 0 | 0 | 0 | Repositories: one module per table family over ``BaseRepository``. |
| [`database.repositories.account_plugins`](modules/database.repositories.account_plugins.md) | 1 | 0 | 8 | AccountPluginsRepository — direct account→plugin assignments. |
| [`database.repositories.accounts`](modules/database.repositories.accounts.md) | 1 | 0 | 7 | AccountsRepository — the persistent account registry (spec §1). |
| [`database.repositories.base`](modules/database.repositories.base.md) | 1 | 1 | 6 | Shared plumbing for every repository over the async connection. |
| [`database.repositories.cache`](modules/database.repositories.cache.md) | 1 | 0 | 5 | Per-account persistent cache of Discord entities (spec add-on: "plugins |
| [`database.repositories.cooldowns`](modules/database.repositories.cooldowns.md) | 1 | 0 | 9 | Persistent cooldown repository (spec §21/§22). |
| [`database.repositories.credentials`](modules/database.repositories.credentials.md) | 1 | 0 | 4 | Encrypted-credential repository (spec §12/§13). |
| [`database.repositories.kv`](modules/database.repositories.kv.md) | 1 | 0 | 6 | KvRepository — tiny generic key/value store (misc persistent state). |
| [`database.repositories.logs`](modules/database.repositories.logs.md) | 2 | 2 | 10 | AuditRepository + LogRepository — the two bounded append-only trails. |
| [`database.repositories.plugin_profiles`](modules/database.repositories.plugin_profiles.md) | 1 | 1 | 7 | PluginProfilesRepository — per-plugin named setting profiles (v7). |
| [`database.repositories.plugins`](modules/database.repositories.plugins.md) | 1 | 0 | 10 | PluginRepository — plugin manifests, per-account states and settings. |
| [`database.repositories.rate_policies`](modules/database.repositories.rate_policies.md) | 1 | 0 | 8 | RatePolicyRepository — the 4-level rate policy store (spec §5). |
| [`database.repositories.settings`](modules/database.repositories.settings.md) | 1 | 0 | 4 | AccountSettingsRepository — per-account framework settings (key -> value). |
| [`database.repositories.sync_groups`](modules/database.repositories.sync_groups.md) | 1 | 1 | 12 | SyncGroupsRepository — first-class sync group rows (spec: sync groups). |
| [`database.repositories.tasks`](modules/database.repositories.tasks.md) | 1 | 0 | 5 | TaskHistoryRepository — persistent finished-task archive (spec §6). |
| [`database.schema`](modules/database.schema.md) | 0 | 1 | 1 | Ordered SQL migrations. Append-only: NEVER edit an applied migration. |
| [`database.telemetry`](modules/database.telemetry.md) | 1 | 0 | 7 | FINAL-PHASE (G1/R1): boundary DB worker telemetry — R1 mitigation. |
| [`interfaces`](modules/interfaces.md) | 0 | 0 | 0 | User-facing interface packages: `cli` (REPL + one-shot) and `dashboard`. |
| [`interfaces.cli`](modules/interfaces.cli.md) | 0 | 0 | 0 | CLI interface package: shared command engine + REPL/one-shot surface. |
| [`interfaces.cli.commands`](modules/interfaces.cli.commands.md) | 2 | 46 | 46 | Shared CLI command engine (spec §13) — used by the REPL and one-shot mode. |
| [`interfaces.cli.repl`](modules/interfaces.cli.repl.md) | 1 | 2 | 6 | Interactive REPL + one-shot mode for the CLI surface (spec §13). |
| [`interfaces.dashboard`](modules/interfaces.dashboard.md) | 0 | 0 | 0 | interfaces.dashboard — HTTP+WebSocket dashboard (spec §12, agent A8). |
| [`interfaces.dashboard.server`](modules/interfaces.dashboard.server.md) | 4 | 19 | 47 | Dashboard backend: JSON REST + WebSocket live push over ControlAPI. |
| [`interfaces.dashboard.ws`](modules/interfaces.dashboard.ws.md) | 2 | 8 | 8 | Minimal RFC 6455 WebSocket server subset (text frames only). |
| [`main`](modules/main.md) | 0 | 4 | 4 | LibzentSuite entrypoint. |
| [`plugins`](modules/plugins.md) | 0 | 0 | 0 | Plugins directory — each subdir is a framework plugin. |
| [`plugins.ping`](modules/plugins.ping.md) | 0 | 0 | 0 | ping plugin: sends a provided message to a chosen channel. |
| [`plugins.ping.plugin`](modules/plugins.ping.plugin.md) | 1 | 0 | 11 | Ping plugin: sends a provided message to a chosen channel. |
| [`plugins.probe`](modules/plugins.probe.md) | 0 | 0 | 0 | Probe fixture plugin: the framework's reference test subject. |
| [`plugins.probe.plugin`](modules/plugins.probe.plugin.md) | 1 | 0 | 14 | Probe plugin: discovery/attachment/command-registry test fixture. |
