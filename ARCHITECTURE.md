# LibzentSuite — Architecture

Implemented exactly per the architecture in `TODO + Ideas.md` (§"Suggested
architecture", §14 project structure). Inter-module contracts: INTEGRATION.md.

```
                    CLI / GUI
        (interfaces/cli)   (interfaces/dashboard)
                         |
                         v
                   Core API  (core.api.ControlAPI — ONE facade)
                         |
     +---------+---------+---------+---------+
     |         |         |         |         |
 Account   Profile    Sync     Plugin   Settings
 Manager   Manager    Group     Manager  Manager
 (core.    (core.     Manager   (core.   (core.
  (core.              (core.    (core.
  manager)  manager)  sync_       manager) manager)
     |         |       group_      |         |
     |         |       manager)    |         |
     +---------+---------+---------+---------+
                         |
                  Task Scheduler (core.task_scheduler)
                         |
              Rate Limit Manager (core.rate_limit_manager)
                         |
                   Discord Adapter (adapters/discord | adapters/simulator)
                         |
                      Database (database/ — models, migrations, repositories)
```

Cross-cutting (every manager consumes them):
`core.event_bus` (typed pub/sub), `core.logging_service` (structured logs +
stdlib bridge), `core.audit` (interaction trail), `core.credentials` (.env
only), `core.bootstrap` (composition root).

## Design principle (spec: "IMPORTANT DESIGN DESIGN PRINCIPLE")

Plugins never manage accounts, the database, GUI/CLI state, or global rate
limiting. They request services: `ctx.services.send_message(...)` submits a
task, `collect_messages(...)` reads the bus, `validate_channel(...)` checks
the cache. Everything else (pacing, serialization, retries, persistence,
auditing) is the framework's job.

## First-class Account (spec §1)

`Account` splits persistent state (`database.models.AccountRecord`: id, name,
token ref, enabled, sync group, settings) from runtime state
(`AccountRuntime`: client, connection status, DiscordCache, latency, uptime,
supervisor). The details page (spec §8) draws runtime data EXCLUSIVELY from
the live client's cached snapshot — zero extra fetches.

## Capability model (revises spec §4)

**Current (implemented): `Account → Plugins`, direct assignment.** Which
plugins an account runs is decided by rows in the `account_plugins` table
(`AccountPluginsRepository`), mutated through `AccountManager.
assign_plugin` / `unassign_plugin` / `set_account_plugins` and surfaced by
`ControlAPI.accounts_plugin_*`. Every assignment mutation persists, then
re-attaches the account's plugins **immediately** — there are no
"applies on next start" semantics. Assignment is *not* enablement: the
assignment says which plugins attach to the account; the per-account
enabled/disabled flag (`plugins enable|disable`, `plugin_states` table)
says whether an attached plugin actually runs. An account with no
assignments runs zero plugins (fail-closed).

**Profiles: retained future infrastructure, no capability effect.**
`ProfileManager` and the `profiles` / `profile_plugins` / `account_profiles`
tables still exist and their CRUD + assignment bookkeeping still works from
both surfaces, but nothing consults them when attaching plugins — assigning
a profile never changes which plugins an account runs.

**Future (NOT implemented):** plugin-specific profiles / multiple saved
configurations per plugin. When that lands, profiles will describe
per-plugin configuration sets; until then they are records only.



## Sync groups

`SyncGroupManager` (core/sync_group_manager) owns PERSISTENT group
membership: first-class group rows in `sync_groups` (id = stable identity,
unique name, description, enabled), membership itself on
`accounts.sync_group` (an account is in at most one group). It is the only
writer of membership and cooperates with — never absorbs — its neighbours:
`RateLimitManager` stays the sole owner of RUNTIME pacing (grouped
accounts share the `group:<name>` clock exactly as before; the manager
just keeps the engine's account→group metadata in sync via the existing
`set_account_group` API). `AccountManager` stays the account lifecycle
owner and delegates the group bits of add/remove/restore to the group
manager (AccountManager → SyncGroupManager → RateLimitManager). Deleting
a group never deletes accounts; a non-empty group must be emptied (or
explicitly detached) first. Migration v5 backfills one group per DISTINCT
legacy `accounts.sync_group` value, and legacy values keep working
(get-or-adopt).

## Rate limiting (spec §5)

Policies live at four levels (global < account < plugin < account+plugin).
`effective()` = the strictest (MAX interval) across applicable levels.
Pacing engine: per-(account, action) minimum interval + uniform jitter,
priority-ordered waiters, injectable clock; server 429s set account or
fleet-wide cooldowns that every acquire respects. Hard floor 1.5s/action,
raise-only, with intervals and floor capped at 86400s (1 day);
`warnings()` reviews aggressive/jitter-less configurations.

## Task pipeline (spec §6)

One worker lane per account (concurrency = 1 — never two in-flight requests
for one account), cross-account priority fairness, cooperative cancellation,
timeouts, retry/backoff (transient + ServerRateLimit; FlowBlocked fails fast
with the reason), exactly-once terminal bookkeeping: history row, audit row
(wait_ms/jitter_ms/attempts), bus event.

## Testing (spec §9)

`adapters/simulator` swaps the Discord boundary for an in-memory world; the
simulation runner composes a complete ephemeral framework per scenario.
`tests/unit` (per-module), `tests/integration` (production bootstrap over the
simulator), `tests/simulation` (scenario suite). 1094+ tests, no network, no
tokens.

## Boot flow

```
.env -> Credentials -> config/ -> Database(+migrations) -> LogService
  -> EventBus -> RateLimitManager(config policies) -> SyncGroupManager
  -> SettingsManager -> AuditRecorder
  -> Task Scheduler(late-bound sender) -> PluginManager
  -> AccountManager(restores persisted accounts,
     delegates group-membership restore to SyncGroupManager)
  -> sender bound -> scheduler.start() -> SimulationRunner -> ControlAPI
  -> {REPL | one-shot CLI | Dashboard | headless}
```

Every mutation from either surface goes through ControlAPI, so the CLI and
the dashboard can never diverge.
