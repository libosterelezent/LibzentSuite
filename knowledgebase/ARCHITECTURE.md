# SlaughterMudae — Architecture (source-verified)

This file documents the *real* architecture as implemented in the repository:
the component topology, the audited API boundary contract between framework
and Discord, the rich event model at the adapter boundary, the who-calls-whom
rules, the dependency directions (verified by grep, not by assumption), and
the exact composition order in `core.bootstrap.build`. It goes deeper than
the condensed root `ARCHITECTURE.md` and cross-checks it; anything the root
docs get wrong is listed in the session report (consolidated into
DISCREPANCIES.md). Companion files: [COMPONENT_MAP.md](COMPONENT_MAP.md) for
per-component detail, [DATA_FLOW.md](DATA_FLOW.md) for traffic flows,
[RUNTIME_LIFECYCLE.md](RUNTIME_LIFECYCLE.md) for boot/shutdown.

## The component topology

```
                        ┌──────────────────────────────────────────────┐
                        │            MANAGEMENT SURFACES               │
                        │ interfaces.cli (REPL/one-shot)              │
                        │ interfaces.dashboard (REST + WS SPA)        │
                        │ core.remote_control (@mention from Discord) │
                        └───────────────────────┬──────────────────────┘
                                                │  the ONLY entry point:
                                                ▼  core.api.ControlAPI
                        ┌──────────────────────────────────────────────┐
                        │                 FRAMEWORK                    │
                        │  AccountManager ── PluginManager            │
                        │       │   \──────── ProfileManager           │
                        │       │        SettingsManager (all layers)  │
        inbound ───────►│  AccountManager._wire_client callbacks       │
        (gateway events)│       │                                      │
                        │  TaskScheduler (1 lane/account, retries)    │
                        │       │ sender = AccountManager.send_hook    │
                        │       ▼                                      │
                        │  RateLimitManager (floor/pacing/jitter/     │
                        │  learned defaults/429 cooldowns+telemetry)   │
                        │                                              │
                        │  SelfCommandHub (per-account command        │
                        │    registry ← PluginManager, → live clients)│
                        │  AccessManager (admin ids, per-account      │
                        │    command perms + prefixes, kv-backed)     │
                        └───────┬──────────────────────────┬───────────┘
                                │ demand-driven only        │ events/state
                                ▼                           ▼
                        ┌───────────────┐          ┌──────────────────┐
                        │    PLUGINS    │          │  EventBus +      │
                        │ decide WHEN a │          │  LogService +    │
                        │ call is needed│          │  AuditRecorder + │
                        └──────┬────────┘          │  SQLite (repos)  │
                               │ ctx.services.*   └──────────────────┘
                               │ (submit tasks)
                               └────────► back into TaskScheduler

                                ┌───────────────────────────────┐
                                │   ADAPTER BOUNDARY            │
        RateLimitManager grant ─► adapters.base.DiscordClient    │
        (protocol):             │   /            \               │
                                │ RealDiscordClient  SimClient   │
                                │ (discord.py-self)  (SimWorld)  │
                                └───────────────────────────────┘
```

The root README's summary holds in source: *SlaughterMudae controls the flow;
plugins control API usage; the RateLimiter controls when those calls may
execute.*

## The API boundary contract

The audited, enforced contract lives verbatim in the module docstring of
`core/account_manager/manager.py` ("API BOUNDARY CONTRACT (architecture
spec) — audited & enforced"). The framework (event routing, account control,
task orchestration, lifecycle) **never initiates a Discord API call for its
own purposes**. The COMPLETE inventory of client touches, each demand-driven:

1. `connect()` / `disconnect()` — gateway lifecycle (account control).
2. `client.cached_snapshot()` — memory-only read of what the client already
   holds; **zero API calls**; the account details page draws exclusively
   from it.
3. `send_hook` → `client.send_message` — executes a task a **plugin**
   submitted (paced by the RateLimiter before it runs).
4. `resolve_dm_channel` → `client.open_dm` — **only** on a DM-send cache
   miss, for a plugin-demanded send.
5. `AccountPluginServices.fetch_user` → `client.fetch_user` — plugin-facing,
   on-demand, DB-cached per account.
6. `AccountManager._maybe_typing` → `client.send_typing` — the ONE
   deliberate, opt-in exception: a send-decoration policy
   (`typing_probability`, default `0.0` = never). No plugin demands it, so it
   ships disabled.

There is no prefetching, no "just in case" fetching, and no API-derived state
beyond what a live send/flow actually consumed. Rate-scope metadata
(`AccountManager._sync_group_scope` feeding
`RateLimitManager.set_account_group`) is pure plumbing — no API involved.
Capability actions (`client.add_reaction`, `client.click_button`) are
plugin-demanded pipeline tasks like sends (emoji / `custom_id` ride in
`Task.content`; the adapter resolves the live message on demand).

## The rich event model boundary (`adapters/model.py`)

One conversion at the boundary, project-owned forever after:

* `adapters.model.convert_message(message, *, dm, viewer_id)` is **pure and
  non-blocking** — it reads only what the library event already holds;
  missing optionals stay `None` rather than triggering fetches. The adapter
  (not the converter) decides the `dm` flag (`RealDiscordClient.connect`'s
  `on_message` uses `isinstance(message.channel, discord.DMChannel)`).
* The result is a frozen `adapters.model.DiscordMessage`: the five original
  flat fields (`id`, `channel_id`, `author_id`, `content`, `dm`) keep their
  exact order and names (compatibility surface used by soulcopy, roll,
  remote control, scheduler summaries, sim scenarios); rich additions are
  all defaulted — `guild_id`, `author` (`DiscordUser`), `channel`
  (`DiscordChannel`), `embeds` (`DiscordEmbed` with `field_map()`),
  `components` (`DiscordComponent` with `walk()/find()`,
  `DiscordMessage.buttons/button(custom_id)` for kakeraloot),
  `attachments`, `reactions`, `reference` (`DiscordMessageReference` — ids
  only, `referenced` populated only when the library already cached it),
  `created_at`.
* `raw` is the documented escape hatch (`DiscordMessage.unwrap()`):
  runtime-only (never serialized into DB/tasks/caches), `repr=False` (logs
  can never dump library objects), optional (`None` for synthetic/simulator
  messages).
* Core never sees the adapter types on the bus: `core.event_bus.MessageReceived`
  keeps the flat fields and carries the rich DTO on `message: Any`
  (explicitly typed `Any` "so core never imports the adapter layer").
  `convert_reaction` similarly returns a plain dict because "the event layer
  is core-side and must not import adapter types".
* Events carry **data only**: no rate limiting, sleeping, retrying or
  scheduling lives in the model; actions flow Plugin → PluginServices →
  scheduler → limiter → adapter, exactly like sends.

## Who calls whom — the rules

1. **Surfaces → ControlAPI, nothing else.** `interfaces.cli.commands.run_command`
   "calls ONLY `core.api.ControlAPI` methods"; the dashboard's REST routes map
   1:1 to ControlAPI; the self-command layer's Framework verbs reach the same
   API through the bootstrap-registered `COMMAND_HUB.api_provider`. Parity by
   construction.
2. **ControlAPI → managers** (duck-typed delegation; no manager imports at
   runtime — `core/api.py` imports manager types only under `TYPE_CHECKING`).
3. **Plugins → PluginServices only.** `core.plugin_base.PluginBase` plugins
   never touch a client, the limiter, or the bus directly; the per-(account,
   plugin) facade is `core.account_manager.services.AccountPluginServices`,
   built by `AccountManager.services_for` via a factory handed to
   `PluginManager.activate_account`.
4. **PluginServices → TaskScheduler** (`submit`) for every outbound action;
   the scheduler's sender is `AccountManager.send_hook`. Sends await task
   completion through `TaskAckRegistry` (bus `Task*` events → futures).
5. **TaskScheduler → RateLimitManager** (`acquire` before every attempt,
   `report_server_limit` on 429) **→ sender → adapter**. The scheduler
   treats the limiter and sender duck-typed.
6. **Adapters never call the framework.** They fire callbacks
   (`on_message`, `on_disconnect`, `on_reaction`, `on_message_edit`,
   `on_message_delete`) that `AccountManager._wire_client` assigns **before**
   `connect()` — the pinned ordering.
7. **The bus is the only fan-out.** Adapters → account manager callbacks →
   `EventBus.publish_nowait` → subscribers (audit-in, plugin dispatch, remote
   control, event logging, dashboard task events, ack registry). One failing
   handler never breaks others (`EventBus.publish` isolation).
8. **Managers collaborate duck-typed**, never by import:
   `core/account_manager/manager.py`: "`plugin_manager` / `scheduler` /
   `limiter` … are called ONLY via the surfaces pinned in INTEGRATION.md …
   Nothing imports those packages at runtime."

## Dependency directions (verified)

Generated inventory: `knowledgebase/generated/module_dependencies.json`.
Verified by grep over `from adapters|import adapters` in `core/`:

* **core → adapters is the forbidden direction at runtime.** Status:
  * `core/event_bus.py` — avoids it entirely (`message: Any`).
  * `core/plugin_base.py` — `adapters.base.DiscordMessage` only under
    `if TYPE_CHECKING:` (no runtime import).
  * `core/bootstrap.py` — `adapters.discord.RealClientFactory` and
    `adapters.simulator.SimulationRunner` are imported **lazily inside
    `build()`** (function level), and both are injectable parameters
    (`client_factory=`, `simulation_runner=`) — tests/sims never import the
    live adapter.
  * **`core/account_manager/manager.py:83` violates the strict rule**:
    `from adapters.base import DiscordMessage` is a module-level *runtime*
    import. It only pulls the dependency-light protocol module (no
    `discord.py`), so no library coupling leaks into core, but it is a real
    core→adapters import edge (reported for DISCREPANCIES.md).
* **adapters → core is allowed and used**: `adapters/discord/client.py`
  imports `core.credentials` and `core.errors` (the shared domain-error
  vocabulary is how the scheduler reacts to library failures without knowing
  the library); `adapters/simulator/*` import `core.errors`.
* **database → core (errors only)**; core managers → database models/repos
  duck-typed. `database/models.py` is deliberately dependency-free.
* **`core.priority.Priority`** lives in its own module precisely so the
  limiter and scheduler need not import each other.
* **Everything flows through composition**: `core.bootstrap.build` is "the
  only place that knows how the managers interconnect".

## Composition order (`core.bootstrap.build`)

Verified line by line; see [RUNTIME_LIFECYCLE.md](RUNTIME_LIFECYCLE.md) for
the full annotated sequence. Abbreviated:

```
Credentials.from_file(env_file)                  # tokens/ids, .env only
load_config(config_dir)                          # global settings, policies, account files
Database(db_path)                                # sqlite + migrations in the constructor
LogService() + attach_db(database.logs)          # structured logs persist
attach_stdlib_bridge(logs)                       # slaughtermudae.* stdlib records ingested
EventBus() + _wire_event_logging(bus, logs)      # lifecycle events -> structured lines
RateLimitManager(bus, svc_logger, database.policies,
                  kv_repo=database.kv)           # floor + stored policies
                                                 # (learned defaults fill unpolicied actions)
_apply_policies(limiter, config)                 # config file policies (floor still applies)
SettingsManager(bus, svc_logger, plugin_repo=database.plugins,
                account_settings_repo=database.account_settings,
                global_layer=config.global_settings)
AuditRecorder(database.audit)
sender_box = {"send": None}                      # LATE-BOUND SENDER SEAM
TaskScheduler(bus, svc_logger, limiter, _sender, audit=audit,
              history_repo=database.tasks)
COMMAND_HUB.pacing = PacingBackend(limiter)      # prefixrate verb -> limiter bridge
access = AccessManager(database.kv, credentials) # admin panel store (kv rows)
await access.migrate_from_credentials()          # ONE-TIME OWNER_USER_IDS -> admin list
await access.warm_prefix_cache()                 # per-account prefix configs (factory cache)
COMMAND_HUB.access = access                      # adminlist/adminperms/adminprefix verbs
PluginManager(...) + discover()                  # binds itself to COMMAND_HUB (last-wins)
ProfileManager(database.profiles, logger, database.accounts)
client_factory ||= RealClientFactory(credentials, # lazy import, injectable;
                    command_prefix=env default,  # per-account admin-panel prefixes
                    access_manager=access)       # override this default
AccountManager(...)                               # ctor restores persisted accounts
sender_box["send"] = accounts.send_hook           # seam filled BEFORE start
for payload in config.accounts: accounts.add_account(...)
scheduler.start()
simulation_runner ||= SimulationRunner(plugin_dir, credentials)   # best-effort
api = ControlAPI(..., access=access)
COMMAND_HUB.api_provider = lambda: api            # Framework verbs -> the ONE API
return Framework(...)
```

The scheduler↔accounts cycle (scheduler needs `accounts.send_hook`; accounts
needs the scheduler for submits/cancels) is broken by the late-bound
`sender_box` — the same seam `adapters.simulator.runner.SimulationRunner.run`
uses. The SimulationRunner is constructed before the ControlAPI. (The
retired RemoteControl bridge is no longer wired in production — only the
simulator stack constructs it, for the `control_mention` scenario.)

## Failure-isolation spine (why the topology holds)

* One failing bus handler is logged and skipped (`EventBus.publish`).
* One crashing plugin loses only its dispatch (`PluginManager._dispatch`)
  and is disabled on lifecycle-hook failure (`PluginManager._activate_one`).
* One account's dead flow aborts via the `core.errors.FlowBlocked` family
  (`TargetGone`, `AccessDenied`, `CacheInvalid`, `FetchRequired`) while
  every other account keeps running; plugins must catch it, log one clean
  line, and return (the soulcopy contract).
* The scheduler swallows every sender exception into a task failure — "the
  scheduler NEVER crashes"; history/audit write failures are isolated.
* Logging, audit, and policy persistence are best-effort by design; a
  failing sink counts failures (`LogService.stats`) instead of recursing.

## The admin access model (`core/access_manager.py`)

`AccessManager` replaces the env-`OWNER_USER_IDS` authorization model
with DB-backed, per-account, fail-closed state in kv rows under the
`access:` prefix:

* `access:admins` — the framework admin id list. Admins may command ANY
  account and use the admin-only framework verbs.
* `access:account:<name>` — `{is_admin, prefix_mode (mention|symbol|
  both), prefix_symbol, allowed_user_ids, user_id}`: who may invoke THIS
  account's commands, how it is addressed, and whether the account's
  own user id carries the admin bypass (the account manager records
  `user_id` at connect via `set_account_user_id`).
* `access:mudae_bot_id` — the Mudae bot id, DB-first with the env
  `MUDAE_BOT_ID` as fallback.

**The gate** (`can_invoke(account, user_id)`): admin bypass → per-account
allowlist → deny. An empty allowlist admits NOBODY but admins
(fail-closed; an unknown/absent row behaves as empty). The adapter's
`allowed_invoker_async` gates every self-command dispatch and the plugin
manager's `execute_self_command` re-checks (defense in depth); both
fail closed on checker errors.

**One-time env migration** (`migrate_from_credentials`, run by the
bootstrap): when the stored admin list is empty it is seeded from
`OWNER_USER_IDS`, after which the env value is IGNORED for
authorization. Plugins see credentials through
`AccountPluginServices.credentials`, which returns the injected
credentials view (the env `Credentials` object in the live bootstrap
stack); the former `core.framework_credentials.FrameworkCredentials`
wrapper was never constructed anywhere and has been removed.

Surfaces: dashboard Admin tab (`/api/admin/*`), the admin-only
self-command verbs (`adminlist`, `adminperms`, `adminprefix`). Flow
trace: [DATA_FLOW.md](DATA_FLOW.md) (f).

## The self-command registry (`SelfCommandHub`, `core/plugin_manager/commands.py`)

The per-account command surface connects three parties without new
wiring in the account manager:

* the **PluginManager** (registry SOURCE) publishes an account's spec
  list whenever assignments or enabled-flags change (the same paths
  that re-attach plugins);
* each account's **live Discord client** (registry CONSUMER) subscribes
  on connect and rebuilds its `ext.commands` Bot registrations
  (`user_bot=True` — any allowed invoker may type the command; the
  AccessManager gate enforces ownership fail-closed);
* **invocations** flow back through `SelfCommandHub.invoke` →
  `PluginManager.execute_self_command`, which parses declarative
  `ArgSpec`s, builds the `CommandContext`, runs the handler with
  plugin-grade isolation and sends the reply through the per-plugin
  services facade (paced pipeline, never a raw client send).

Framework verbs (`pg`, `say`, dynamic plugin-grouped `help`, Settings:
`prefixrate`, `adminlist`, `adminperms`, `adminprefix`) are declared in
the same module as first-class `CommandSpec`s; their names are reserved
(collision policy: first registration wins, later ones dropped with a
warning). `PacingBackend` (registered by the bootstrap) bridges the
`prefixrate` verb to the limiter. The hub is a process-wide singleton;
managers bind/unbind (last-wins, documented for test isolation).

## Learned pacing + delay-free scheduling (`core/rate_limit_manager`)

* **`DEFAULT_ACTION_INTERVALS`** — when NO policy is stored for an
  action the engine falls back to measured zero-throttle rates:
  `message` 2.6 s, `dm` 2.6 s, `reaction` 2.5 s, `component` 3.0 s.
  These are FALLBACKS, not policy: stored policies compose on top with
  safest-wins (max) semantics, so a per-account `prefixrate` override
  is authoritative whenever it is the only stored layer. Discord's own
  per-route bucket limits (read by the library on write routes) are the
  ACTUAL pacing constraint; the hard floor (`MIN_ACTION_INTERVAL`)
  dropped to 0.05 s accordingly (an absolute sanity minimum against
  zero-delay tight loops, not a safety claim).
* **429 telemetry** — user accounts receive no `X-RateLimit` headers on
  success responses, so per-route bucket state was never observable;
  429 hits ARE. `hits_429` counts per-account throttle hits + a fleet
  total (surfaced in `ratelimit_stats()` and the dashboard's Policies
  tab card; reset with the account's cooldowns).
* **Delay-free scheduling** — retries carry NO scheduler-owned delay:
  they re-enter `limiter.acquire` immediately and the limiter's server
  cooldown + interval own every delay (pacing is exclusively the
  limiter's job; the old scheduler backoff sleeps are gone).
* **Fleet send lane REMOVED** — redundant with sync groups and a
  single-account throughput throttle. Per-account pacing + sync groups
  + the floor are the pacing layers; the `ratelimit lane` routes/CLI
  answer a compat stub (`{"removed": true, ...}`).

## The UI extension API (`ui_stats` / `ui_panels`)

Plugins extend the dashboard through two optional hooks on
`PluginBase`, satisfied with STRUCTURED data (the dashboard's own
templates render and `esc()` every value — XSS-safe by construction):

* `ui_stats(ctx)` → `StatCard`s rendered as Home-tab metric cards
  (`GET /api/ui/stats?account=`; per (account, plugin), isolated — a
  crashing provider contributes nothing);
* `ui_panels(ctx)` → `UIPanel`s rendered by
  `interfaces/dashboard/static/js/plugin-ui.js` as sidebar nav entries
  + content areas (framework-level: first instance per plugin wins,
  deduped by id). Sections are `metrics`/`table`/`list`/`text` kinds.

Contract: [PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md) §7b; endpoint shapes:
[API_ENDPOINTS.md](API_ENDPOINTS.md) §5; flow trace: [DATA_FLOW.md](DATA_FLOW.md) (e).
