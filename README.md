# SlaughterMudae

A multi-account Discord automation framework designed around one principle:
**your accounts should behave like well-paced humans, not bots.**

Built for Mudae farming with any number of accounts that share a pacing
clock so they look like one user, back off instantly when
Discord says so, and never make an unnecessary API call — all managed from
a local dashboard.

> ⚠️ Selfbot usage violates Discord's Terms of Service and can get accounts
> suspended. The safety systems (learned pacing defaults, human-like
> jitter, sync-group serialization, always-on server-limit handling)
> **mitigate** risk — they do not authorize anything. Keep policies
> conservative and only use accounts you can afford to lose.

---

## Contents

1. [What it does](#what-it-does)
2. [Install](#install)
3. [Quick start](#quick-start)
4. [The dashboard](#the-dashboard)
5. [Setting up accounts](#setting-up-accounts)
6. [Plugins](#plugins)
7. [Everyday commands](#everyday-commands)
8. [Safety model](#safety-model)
9. [Troubleshooting](#troubleshooting)
10. [Reference](#reference)

---

## What it does

- **Plugin framework** — self-describing plugins (settings manifest,
  lifecycle, declared self-commands, dashboard UI extension) discovered
  from a directory; the framework ships with NONE bundled, so `plugins/`
  may stay empty
- **Plugin profiles** — reusable per-plugin setting bundles (with channel
  allow/deny scopes) an account binds per assigned plugin; effective
  settings merge defaults ← profile ← per-account overrides
- **Admin panel** — DB-backed command authorization (per-account Discord
  user-id allowlists, framework admin list, per-account command prefix
  mention/symbol/both, Mudae bot id) replacing the `OWNER_USER_IDS` env
  model, with a one-time migration
- **Turn-taking across accounts** — shared sync-group clocks make N
  accounts emit the cadence of ONE careful user; cooldowns persist
  across restarts
- **Anti-detection pacing** — learned per-action defaults (measured
  zero-throttle rates) composed with your policies; strictest wins
- **Everything from one dashboard** — accounts, tokens (encrypted),
  plugins + profiles, rate policies, live logs, audit trail, 429
  telemetry, simulation
- **Fully offline-testable** — a complete Discord simulator runs the whole
  stack with zero network

## Install

```bash
git clone <this repo> && cd SlaughterMudae
pip install -r requirements.txt       # aiosqlite + cryptography + discord.py-self
cp .env.example .env                  # then edit it (see below)
chmod 600 .env
```

`.env` needs:

```ini
MUDAE_BOT_ID=432610292342587392       # Mudae's public bot id (DB setting wins once set in the Admin tab)
OWNER_USER_IDS=123456789012345678     # YOUR Discord user id — migrated ONE TIME into the DB admin list
DASHBOARD_TOKEN=pick-a-long-random-string
```

Account tokens are **not** in `.env` — they're stored encrypted in the
database (see [Setting up accounts](#setting-up-accounts)).

## Quick start

```bash
# 30-second offline tour — no Discord account needed
python3 main.py --sim-list              # list scenarios
python3 main.py --sim soulcopy_basic    # full pipeline over the simulator

# go live
python3 main.py --dashboard --headless --start-all
# → dashboard at http://127.0.0.1:8070
```

Open the dashboard, paste your `DASHBOARD_TOKEN` into the token box (or
use `http://127.0.0.1:8070/?token=<DASHBOARD_TOKEN>` — the SPA strips
it from the URL after seeding), then check the **Admin** tab:

> **One-time migration** — on the first boot the framework seeds the DB
> admin list from `OWNER_USER_IDS` and then IGNORES the env value for
> authorization. From that point on, who can command which account is
> managed in the Admin tab (framework admin ids + per-account
> permission lists, fail-closed). If neither the env key nor the admin
> list has ids, NOBODY can invoke commands until you add an admin there.

Or without the dashboard:

```bash
python3 main.py                        # interactive REPL
python3 main.py accounts list          # one-shot command mode
```

## The dashboard

`http://127.0.0.1:8070` (requires `DASHBOARD_TOKEN`). One page per concern:

| Page | What you do there |
|---|---|
| **Home** | live fleet status + plugin-contributed metric cards |
| **Accounts & Groups** | add/remove, start/stop, store tokens, direct plugin assignment, sync groups |
| **Plugins** | per-account enable/disable, settings, the **profile library** (create/edit/delete setting bundles with channel scopes) |
| **Profiles** | per-account profile bindings + the effective merged-settings view |
| **Activity** | live log stream + every Discord interaction (audit) |
| **Policies** | pacing policies, effective rates, safety warnings, the 429 throttle card |
| **Admin** | framework admin ids, per-account command permissions + prefixes (mention/symbol/both), Mudae bot id |
| **Tools** | task queue/history, offline simulations, emergency stop, shutdown |

Nearly everything works from both (`python3 main.py …` or the dashboard —
same API underneath, so pick whichever you like). Dashboard-only today:
admin-panel management, plugin-profile editing, per-account channel
lists, the effective-settings view and bulk plugin-set; the CLI covers
accounts/credentials/plugins/profiles-bindings/ratelimit/cooldowns/
tasks/simulation/logs/audit/estop/status/groups.

Debugging a live issue? Add `--debug`: DEBUG-level logs, Discord gateway
lines (reconnect/close codes), DB worker timings, scheduler pressure.

## Setting up accounts

1. **Add the account** — dashboard → Accounts → Add (or
   `python3 main.py accounts add myaccount` — name plus optionally the
   plugin(s) it should run; assign more later with `accounts plugin-add`).
   Put it in a **sync group** with `groups set-account myaccount <group>`.
2. **Store its token** — Accounts → credential (or
   `python3 main.py accounts credential myaccount <token>`). The token is
   Fernet-encrypted at rest; the key lives in `state/credentials.key`
   (back that file up — losing it means re-entering tokens).
3. **Start it** — the ▶ button (or `accounts start myaccount`)

Put every account that shares work in the same **sync group** — that's
what makes them share a pacing clock.

## Plugins

The framework itself ships with **no bundled plugins** — `plugins/` can
stay empty and everything still boots (the loader simply discovers zero
plugins). Plugins are self-describing packages: a `PluginBase` subclass
with metadata, a `Setting` manifest, lifecycle hooks, optional declared
self-commands (`COMMANDS` with the `ArgSpec` argument grammar) and
optional dashboard UI extension (`ui_stats` stat cards, `ui_panels`
sidebar panels — structured data only, XSS-safe by construction).
**The plugin development guide is
[`knowledgebase/PLUGIN_SYSTEM.md`](knowledgebase/PLUGIN_SYSTEM.md)**
(§7a self-commands, §7b UI extension; the formal API reference is
`knowledgebase/PLUGIN_API.md`), plus `core/plugin_base.py` and
`docs/plugins/writing_plugins.rst`.

```bash
python3 main.py plugins list               # nothing until you add one
# drop a package under plugins/ (or pass --plugins <dir>), then:
python3 main.py accounts add alpha <plugin>   # name + the plugin it runs
python3 main.py accounts plugin-add alpha <plugin>
```

The test suite exercises the framework through fixture plugins
(`tests/fixtures/plugins/probe` for the loader/manager/command registry/
UI extension; `tests/fixtures/sim_plugins/soulcopy` as the full-stack
simulation workload) — those are tests-only, never discovered by a
production boot.

## Everyday commands

From Discord — self-command verbs on any account you're allowed to invoke
(type them with the account's prefix; default prefix comes from
`SELF_COMMAND_PREFIX`, per-account from the Admin tab):

```
sm!pg                       sm!prefixrate          # this account's send pacing
sm!say hello there          sm!help                # plugin-grouped, paginated
```

Mention-prefix management still works (`@account status`, `@account
estop`), and admins get `adminlist` / `adminperms` / `adminprefix`.

From the shell:

```bash
python3 main.py accounts list|start|stop|remove
python3 main.py accounts plugin-add alpha <plugin>
python3 main.py plugins list
python3 main.py ratelimit effective message alpha
python3 main.py estop                  # fleet-wide halt
python3 main.py --sim pacing_respected # offline sanity check
python3 main.py --bench                # performance benchmarks
```

## Safety model

Pacing composes on every outbound action — the strictest wins:

| Layer | What it governs |
|---|---|
| **Sync-group clock** | accounts in one group serialize against each other |
| **Per-account pacing** | each account's own minimum interval + jitter per action |
| **Learned defaults** | with no policy stored: message 2.6 s, dm 2.6 s, reaction 2.5 s, component 3.0 s (measured zero-throttle rates — fallback, not policy); any other action defaults to **1.2 s** |

Plus: a hard sanity floor nothing can go below, server 429s always earn
paced retries (with per-account + fleet throttle telemetry on the
Policies tab), cooldowns Mudae reports are persisted with absolute
expiry, DM channels resolve once and are cached forever, and the
framework itself makes zero self-initiated API calls — every call is a
plugin action through the pipeline above.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `missing_credential` | store the token (Accounts → credential) |
| `invalid_credential` | token wrong/revoked — re-enter it |
| Account keeps reconnecting | young accounts get culled by Discord; see `--debug` close codes |
| Dashboard unreachable | is `DASHBOARD_TOKEN` set? Port free? |
| "cannot boot" | unwritable `--db` path or malformed config — the message says which |
| Boot warns about permissions | `chmod 600 state/credentials.key .env` |
| Key/DB mismatch warning | `state/credentials.key` doesn't match the stored tokens — restore the key or re-enter tokens |

---
---

# Reference

*Everything below is the detailed technical reference. Nothing above will
change your mental model; everything below is for working ON the framework.*

## Layout

```
core/                  framework managers + shared contracts
  api.py               ControlAPI — the ONE facade both interfaces call
  bootstrap.py         composition root (the boot flow of main.py)
  account_manager/     Account (first-class), lifecycle, send hook, cache,
                       lifecycle governor + waiter registry
  plugin_manager/      discovery, per-account instances, event dispatch
  plugin_base.py       the plugin contract (PluginBase/Context/Services)
  profile_manager/     retained future infrastructure: profile records
                       (NOT the capability model — accounts get plugins
                       directly via account_plugins assignments)
  settings_manager.py  ONE settings schema consumed by CLI + GUI alike
  rate_limit_manager/  policies (global/account/plugin/pair), pacing, 429s,
                       effective-config cache, group clocks
  task_scheduler/      queue + priority + cancel + retries + timeouts,
                       pressure observability
  event_bus.py         typed pub/sub: coalesced bounded dispatch
  logging_service.py   structured logging (one service for everything)
  audit.py             interaction audit trail (waits/jitter per send)
  credentials.py       ids in .env; credential_service.py — DB-encrypted tokens
  config.py            config dir loading
database/              SQLite: models, ordered migrations, repositories,
                       boundary worker telemetry (telemetry.py)
adapters/              Discord boundary: discord/ (live, lazy import) +
                       simulator/ (complete offline world)
interfaces/            cli/ (REPL + one-shot) + dashboard/ (SPA + REST + WS)
plugins/               your plugins (the framework ships with none — the
                       directory may be empty; test fixtures live under
                       tests/fixtures/)
benchmark/             the benchmark subsystem (scenarios, profiling modes)
tests/unit|integration|simulation|stress   ~1800-test offline suite
config/                global settings, rate policies, account files
main.py                entrypoint
```

The dashboard serves a sidebar SPA (Home, Accounts & Groups, Plugins,
Profiles, Activity, Policies, Admin, Tools — plus any plugin-contributed
panels) on `http://127.0.0.1:8070` with live WebSocket push; set
`DASHBOARD_TOKEN` in `.env` to require auth. The complete REST/WS/CLI/
self-command reference is `knowledgebase/API_ENDPOINTS.md`.

Dependencies: Python ≥ 3.10 + `aiosqlite` + `cryptography` (see
`requirements.txt`). Live mode additionally needs the user-account client
fork `discord.py-self` (`pip install discord.py-self`), imported lazily —
simulations/tests never require it.

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
```

**SlaughterMudae controls the flow; plugins control API usage; the
RateLimiter controls when those calls may execute.** The framework's
complete client-touch inventory is audited in the `core/account_manager`
module docstring — every entry is demand-driven by a plugin task.

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

### Rate scopes (two composing layers + learned fallback)

| Scope | Key | Governs |
|---|---|---|
| **per-task** | `(account, action)` | consecutive API calls of ONE account |
| **per-account/group** | `(group:<sync_group>, action)` | group members serialize on ONE shared clock: `A → [limit] → B → [limit] → A` |

Both compose (the strictest wait wins). When no policy is stored for an
action, the learned default applies (message 2.6 s, dm 2.6 s, reaction
2.5 s, component 3.0 s — measured zero-throttle rates). Jitter is drawn
BEFORE admission and slept after the grant, and every spacing clock is
stamped at grant + jitter — the expected wire instant — so consecutive
wire sends stay ≥ the effective interval apart (jitter can never
compress the gap). Server 429s always earn at least one paced retry
(and are counted per account + fleet-wide on the Policies tab);
`FlowBlocked` aborts only that account's flow with a human reason.
Groups are registered automatically from each account's `sync_group`
(pure metadata — no API involved). Retries carry no scheduler delay —
the limiter's cooldown + interval own every wait.

## Lifecycle hardening

Every account runtime carries a **generation counter** bumped on each
ownership change (start/stop/remove/estop). Async lifecycle paths capture
it before an await and validate after — a stale start can never resurrect
a stopped account, an old stop can never clobber a newer start, a stale
supervisor stands down by identity. The **lifecycle governor** rate-limits
start/stop thrashing (per-account cooldowns + global buckets; estop and
shutdown bypass entirely). `start_all` runs as a bounded 4-lane scheduler
with admission pacing and single-flight join. IDENTIFYs are paced by a
process-wide gate with depth-projected fast-fail.

## Rich event model

`adapters/model.py` converts every inbound Discord event ONCE into a frozen
project-owned DTO — embeds, components (buttons/selects), attachments,
reactions, references — plus a runtime-only `raw` escape hatch
(`message.unwrap()`). Typed bus events (`MessageReceived`, `MessageEdited`,
`MessageDeleted`, `ReactionAdded`) are additive. Actions are capabilities
on `PluginServices` (`send_message`, `send_dm`, `add_reaction`,
`click_button`) through the paced pipeline; plugins also get
framework-managed cooldowns and persistent per-plugin state.

Plugins subscribe through a **per-account waiter registry**: a waiter for
account A structurally never runs for B's events.

## Rate policies

Four levels — `global`, `account`, `plugin`, `account_plugin` — each
`{interval: seconds, jitter: [min, max]}`. Effective pacing is the MAX
across applicable levels (and the learned per-action default when no
policy is stored). Policies below the current safety floor are rejected
(`UnsafePolicyError`); the floor only rises and persists across
restarts. The composed result is memoized per `(action, account,
plugin)` under a config generation counter (tuple-keyed, capped,
invalidated at every mutation). The `prefixrate` self-command sets the
account's message interval at runtime.

## Credentials & security

Tokens: Fernet-encrypted in the DB; master key outside it
(`state/credentials.key` 0600 or `SM_MASTER_KEY`). Boot audits permissions
on db/key/.env, tightens WAL sidecars, and runs a key/DB decrypt canary.
Dashboard auth is constant-time token compare + loopback-only origin
checks; WS frames and bodies are size-capped; diagnostic artifacts are
0600 and value-free (audited — no token material can reach benchmark/
profile/tracemalloc output).

## Simulation & benchmarks

`adapters/simulator/` is a complete in-memory Discord world (users, channels,
DM pairs, history, a scripted responder, one-shot 429s, channel deletion).
`SimulationRunner` boots a FULL ephemeral framework per run (with its own
temp master key — never touching production state) and produces a timed
execution log, the audit trail and pass/fail checks. Scenarios:
`soulcopy_basic`, `soulcopy_multichunk`, `pacing_respected`,
`server_429_respected`, `channel_gone_aborts`, `control_mention`,
`estop_halt`.

`benchmark/` (see `benchmark/README.md`): scenarios incl. cold boot,
N-account start/stop, restart churn, message bursts; loop-lag
histograms; leak verdicts from trend analysis; `--repeat` medians,
`--profile` (main-thread cProfile), `--tracemalloc` (allocation sites),
`--capture-spikes` (diagnostic snapshots); baselines + tolerance-aware
compare; DB worker telemetry.

## Management surfaces

- CLI: `accounts|credentials|plugins|profiles|ratelimit|cooldowns|tasks|simulation|logs|audit|estop|status|groups|help|quit`
- Dashboard REST: 1:1 with ControlAPI; WebSocket pushes stats/log/audit/task
  events live (complete reference: `knowledgebase/API_ENDPOINTS.md`)
- Self-commands: framework verbs (`pg`, `say`, `help`, `prefixrate`,
  `adminlist`, `adminperms`, `adminprefix`) + every plugin-declared
  `COMMANDS` verb — gated per account by the admin-panel access lists
- Mention prefix (`mention` prefix mode of the same self-command layer —
  same access gate, replies paced)

All of them hit the same ControlAPI; CLI↔dashboard parity holds for the
verb groups listed above, but admin-panel management, plugin-profile
editing, per-account channel lists, the effective-settings view and bulk
plugin-set remain dashboard-only.

## Documentation

- `knowledgebase/` — deep knowledge base (handwritten guides + generated
  read-only reference; `generated/*.json` is gitignored). Start at
  `knowledgebase/README.md`; the complete surface reference is
  `knowledgebase/API_ENDPOINTS.md`. Regenerate with
  `python3 scripts/gen_knowledgebase.py [--check]`.
- `docs/` — Sphinx site: `pip install --user sphinx myst-parser` then
  `python3 -m sphinx -W -b html docs docs/_build/html`
- `INTEGRATION.md` — inter-module contracts
- `HARDENING_README.md` — the hardening history (phases 1–2 + round-2)
- `scripts/bench_db.py`, `scripts/bench_hot.py` — perf harnesses
- `benchmark/README.md` — the benchmark subsystem

## Anti-flag hardening summary

One REST call per entity ever (DM/user cache in SQLite, migration 002);
no double-resolution on DM sends; stale-cache healing on TargetGone;
optional typing indicator (default off); chat commands gated by the
admin-panel access lists (fail-closed); N-account IDENTIFY pacing with per-IP
budget; per-account 429 cooldowns with fleet-wide global-limit handling;
reconnect backoff with jitter and flap-escalation; event-loop-friendly
everything (queued persistence, bounded dispatch slices).
