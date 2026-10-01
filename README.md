# SlaughterMudae

Multi-account Discord automation framework built to the architecture in
`TODO + Ideas.md`: modular plugins that request services from a central core,
first-class accounts with a persistent/runtime split, capability profiles,
a four-level rate-policy system, a centralized task pipeline, SQLite
persistence with repositories + migrations, and two fully parity'd management
surfaces (CLI + browser dashboard) over ONE Core API.

> ⚠️ Selfbot usage violates Discord's Terms of Service and can get accounts
> suspended. The 1.5s/action hard floor, human-like jitter ranges and the
> always-on server-limit handling MITIGATE risk — they do not authorize it.
> Keep policies conservative (defaults: 2.0s + 0.0–0.5s jitter) and never add
> accounts you cannot afford to lose.

## Layout (spec §14)

```
core/                  framework managers + shared contracts
  api.py               ControlAPI — the ONE facade both interfaces call
  bootstrap.py         composition root (the boot flow of main.py)
  account_manager/     Account (first-class), lifecycle, send hook, cache
  plugin_manager/      discovery, per-account instances, event dispatch
  plugin_base.py       the plugin contract (PluginBase/Context/Services)
  profile_manager/     capability profiles (plugin bundles per account)
  settings_manager.py  ONE settings schema consumed by CLI + GUI alike
  rate_limit_manager/  policies (global/account/plugin/pair), pacing, 429s
  task_scheduler/      queue + priority + cancel + retries + timeouts
  event_bus.py         typed pub/sub with handler isolation
  logging_service.py   structured logging (one service for everything)
  audit.py             interaction audit trail (waits/jitter per send)
  credentials.py       tokens/ids ONLY in .env
  config.py            config dir loading
database/              SQLite: models, ordered migrations, repositories
adapters/              Discord boundary: discord/ (live) + simulator/ (offline)
interfaces/            cli/ (REPL + one-shot) + dashboard/ (SPA + REST + WS)
plugins/               soulcopy (migrated) + roll (reference plugin)
tests/unit|integration|simulation/   450-test offline suite
config/                global settings, rate policies, account files
main.py                entrypoint
```

## Quickstart

```bash
cp .env.example .env        # fill tokens + OWNER_USER_IDS (chmod 600 .env)
python3 main.py --sim-list             # available offline scenarios
python3 main.py --sim soulcopy_basic   # full pipeline over the simulator
python3 main.py                         # interactive REPL (live accounts)
python3 main.py accounts list           # one-shot command mode
python3 main.py --start-all --dashboard # live accounts + dashboard :8070
bash scripts/run_tests.sh               # the whole 450-test suite
```

The dashboard serves a sidebar SPA (Overview, Accounts, Plugins, Profiles,
Rate Limits, Tasks, Logs, Simulation, Advanced) on `http://127.0.0.1:8070`
with live WebSocket push; set `DASHBOARD_TOKEN` in `.env` to require auth.

Dependencies: Python ≥ 3.10, standard library only. Live mode additionally
needs a user-account client fork (`pip install discord.py-self`), imported
lazily — simulations/tests never require it.

## Architecture: who calls what

```
                    ┌─────────────────────┐
                    │   SlaughterMudae    │
                    │  (core framework)   │
Events ────────────►│ Event routing       │
                    │ Account control     │
                    │ Task orchestration  │
                    │ Lifecycle           │
                    └──────────┬──────────┘
                               │  (events/context only — NEVER API calls
                               │   for its own purposes)
                               ▼
                    ┌─────────────────────┐
                    │       Plugins       │
                    │ Decide WHEN an API  │
                    │ call is actually    │
                    │ required            │
                    └──────────┬──────────┘
                               │  on-demand (services.send_message /
                               │  open_dm / fetch_user)
                               ▼
                    ┌─────────────────────┐
                    │     RateLimiter     │
                    │ · per-task          │  one account's consecutive calls
                    │ · per-account/group │  shared clock per sync_group
                    │ · server-wide lane  │  fleet serialization
                    └──────────┬──────────┘
                               ▼
                         API functions

The core principle: **SlaughterMudae controls the flow; plugins control API
usage; the RateLimiter controls when those calls may execute.** The
framework's complete client-touch inventory is audited in the
`core/account_manager` module docstring — every entry is demand-driven by a
plugin task (the single deliberate exception, the opt-in typing indicator,
ships disabled). Nothing prefetches; nothing fetches "just in case".

## The send pipeline

```
Plugin -> PluginServices.send_message()
        -> TaskScheduler.submit()                    [queued, prioritized]
        -> per-account lane (concurrency = 1)
        -> RateLimitManager.acquire()                [pacing + jitter + 429 cooldowns]
        -> AccountManager.send_hook()                [cache validation]
        -> DiscordClient (real or simulated)
        -> audit + history + events                  [exactly-once per task]
```

On top of the per-account pacing sits the **sequential send lane**
(`core/rate_limit_manager/lane.py`, FarmLite concept made robust): when
enabled, every outbound action from EVERY account is admitted strictly one
at a time, spaced by `prefixrate` (0 = pause the lane, everything holds).
Server-wide serialization + per-account pacing + the 1.5s floor compose —
`ratelimit lane` (CLI) or the Rate Limits page (dashboard) control it, and
the config ships it on by default (`"lane": {"enabled": true,
"prefixrate": 1.5}` in `config/rate_limits.json`).

### Rate scopes (three composing layers)

| Scope | Key | Governs |
|---|---|---|
| **per-task** | `(account, action)` | consecutive API calls (scheduler tasks) of ONE account — a task can never fire back-to-back |
| **per-account/group** | `(group:<sync_group>, action)` | accounts sharing a `sync_group` serialize on ONE shared clock: `A → [limit] → B → [limit] → A` |
| **server-wide lane** | fleet | every outbound action from every account, one at a time, spaced by `prefixrate` |

All three compose (the strictest wait wins), then jitter is applied after
the grant. Groups are registered automatically from each account's
`sync_group` (pure metadata — no API involved).

Retries cover transient failures and server 429s (`ServerRateLimit` always
earns at least one retry, paced by the limiter cooldown — the framework
always respects server-provided rate limits). `FlowBlocked` aborts only THAT
account's flow with a human reason.

**API-call hygiene (anti-flag).** The framework outside plugins makes the
minimum possible Discord REST calls: connections are gateway-only, sends are
plugin-initiated, and every resolved entity is cached per account in SQLite
(`api_cache` — migration 002) so a DM channel or user costs **one call for
the account's lifetime, not one per boot**. DM sends go through the resolved
channel id (never re-fetching the user mid-send), stale cache rows are
dropped when Discord reports a target gone, and an optional typing indicator
(account setting `typing_probability`, default 0 = zero extra calls) mimics
human composing when enabled.

## Rich event model

`adapters/model.py` converts every inbound Discord event ONCE into a frozen
project-owned DTO — content, embeds (fields/author/footer/media),
components (buttons/selects with `custom_id`, nested in action rows),
attachments, reactions, reference ids, author/channel/guild, timestamps —
plus a documented runtime-only `raw` escape hatch (`message.unwrap()`) for
advanced plugins. Typed bus events (`MessageReceived`, `MessageEdited`,
`MessageDeleted`, `ReactionAdded`) are additive: existing plugins keep the
flat field surface, future plugins (snipe/kakerareact/kakeraloot/soulmate
farm) read rich data without re-fetching or core rewrites. Actions are
capabilities on `PluginServices` (`send_message`, `send_dm`,
`add_reaction`, `click_button`) flowing through the paced pipeline — event
models carry data only, never rate limiting.

## Managing from Discord (mention prefix)

Owners mention a running account as the command prefix — `@account status`,
`@account plugins alpha soulcopy off`, `@account estop` — in any channel the
account can see. Replies go through the SAME paced pipeline (lane → pacing →
jitter → audit), non-owners are ignored completely, commands are
allowlisted, and `REMOTE_CONTROL=0` in `.env` disables the bridge entirely.

## Rate policies (spec §5)

Four levels — `global`, `account`, `plugin`, `account_plugin` — each
`{interval: seconds, jitter: [min, max]}`. The effective pacing for an
(account, plugin, action) is the MAX across every applicable level, so no
level can ever make traffic FASTER than a stricter level demands. Policies
below **1.5s/action** are rejected everywhere (`UnsafePolicyError`), the
floor can only be raised, and `ratelimit warnings` reviews the configuration
for aggressive intervals / missing or narrow jitter.

## Credentials & IDs

Everything identifiable lives in `.env` (gitignored, chmod 600): one
`DISCORD_TOKEN_<NAME>` per account, `MUDAE_BOT_ID`, `OWNER_USER_IDS`,
optional `DASHBOARD_TOKEN`. The database stores only the .env key
*references* — token values never leave `.env`.

## Simulation system (spec §9)

`adapters/simulator/` is a complete in-memory Discord world (users, channels,
DM pairs, history, a scripted responder, one-shot 429s, channel deletion).
`SimulationRunner` boots a FULL ephemeral framework per run and produces a
timed execution log, the audit trail and pass/fail checks:

```
0000.089s INFO    scheduler scheduler: queued task 1 (soulcopy:message)
0003.201s INFO    scheduler scheduler: queued task 2 (soulcopy:message)
```

Scenarios: `soulcopy_basic`, `soulcopy_multichunk`, `pacing_respected`,
`server_429_respected`, `channel_gone_aborts`.

## Management surfaces

- CLI: `accounts|plugins|profiles|ratelimit|tasks|simulation|logs|audit|estop|status|help|quit`
  subcommands, interactive or one-shot; identical results everywhere.
- Dashboard API: JSON REST routes mapping 1:1 to ControlAPI methods plus a
  WebSocket pushing stats/log/audit/task events live.

Both interfaces manipulate the exact same accounts, plugins, profiles,
settings, tasks, rate policies, logs and simulation system — parity by
construction, not by discipline.

Inter-module contracts are documented in `INTEGRATION.md`.

## Anti-flag hardening (audit round)

- **One REST call per entity, ever**: DM channels + users resolve through
  memory cache → the per-account `api_cache` table (SQLite, migration 002) →
  a single client call, recorded in both. Restarts never re-fetch.
- **No double-resolution**: DM sends reuse the resolved channel id instead
  of re-running fetch_user/create_dm inside `send_dm`.
- **Stale-cache healing**: a TargetGone on a DM send invalidates the memory
  + DB rows so the next resolution re-opens cleanly.
- **Optional typing indicator**: `typing_probability` account setting
  (default 0 — zero extra API calls); fires `trigger_typing` before guild
  sends when enabled, best-effort only.
- **Mention-prefix management** (`core/remote_control.py`): owner-only,
  allowlisted, self-throttled, replies paced by the full pipeline.

