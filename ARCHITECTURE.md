# SlaughterMudae — Architecture

Implemented exactly per the architecture in `TODO + Ideas.md` (§"Suggested
architecture", §14 project structure). Inter-module contracts: INTEGRATION.md.

```
                    CLI / GUI
        (interfaces/cli)   (interfaces/dashboard)
                        |
                        v
                  Core API  (core.api.ControlAPI — ONE facade)
                        |
         +--------------+--------------+
         |              |              |
  Account Manager  Plugin Manager  Settings Manager
  (core.account_   (core.plugin_   (core.settings_
   manager)          manager)        manager)
         |              |              |
         +--------------+--------------+
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
token ref, enabled, sync group, settings, profiles) from runtime state
(`AccountRuntime`: client, connection status, DiscordCache, latency, uptime,
supervisor). The details page (spec §8) draws runtime data EXCLUSIVELY from
the live client's cached snapshot — zero extra fetches.

## Profiles / capabilities (spec §4)

A profile names a bundle of permitted plugins; accounts get profiles; the
union gates the plugin set at account start (no roles = unrestricted). All
CRUD + assignments persist in SQLite and are reachable from both surfaces.

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
  -> EventBus -> RateLimitManager(config policies) -> SettingsManager
  -> AuditRecorder -> TaskScheduler(late-bound sender) -> PluginManager
  -> ProfileManager -> AccountManager(restores persisted accounts)
  -> sender bound -> scheduler.start() -> SimulationRunner -> ControlAPI
  -> {REPL | one-shot CLI | Dashboard | headless}
```

Every mutation from either surface goes through ControlAPI, so the CLI and
the dashboard can never diverge.
