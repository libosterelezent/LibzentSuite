# SlaughterMudae

A multi-account Discord automation framework designed around one principle:
**your accounts should behave like well-paced humans, not bots.**

Built for Mudae farming with any number of accounts that share a pacing
clock so they look like one user, back off instantly when
Discord says so, and never make an unnecessary API call — all managed from
a local dashboard.

> ⚠️ Selfbot usage violates Discord's Terms of Service and can get accounts
> suspended. The safety systems (1.5s/action hard floor, human-like jitter,
> fleet-wide serialization, always-on server-limit handling) **mitigate**
> risk — they do not authorize anything. Keep policies conservative and
> only use accounts you can afford to lose.

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
  lifecycle, declared self-commands) discovered from a directory; the
  framework ships with NONE bundled, so `plugins/` may stay empty
- **Turn-taking across accounts** — shared sync-group clocks make N
  accounts emit the cadence of ONE careful user; cooldowns persist
  across restarts
- **Anti-detection pacing** — three composing rate layers compose into
  one human-like cadence
- **Everything from one dashboard** — accounts, tokens (encrypted),
  plugins, rate policies, live logs, audit trail, simulation
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
MUDAE_BOT_ID=432610292342587392       # Mudae's public bot id
OWNER_USER_IDS=123456789012345678     # YOUR Discord user id (commands are owner-only)
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
# → dashboard at http://127.0.0.1:8070/?token=<your DASHBOARD_TOKEN>
```

Or without the dashboard:

```bash
python3 main.py                        # interactive REPL
python3 main.py accounts list          # one-shot command mode
```

## The dashboard

`http://127.0.0.1:8070` (requires `DASHBOARD_TOKEN`). One page per concern:

| Page | What you do there |
|---|---|
| **Overview** | live fleet status, pressure, uptime, latency |
| **Accounts** | add/remove, start/stop, store tokens, sync groups |
| **Plugins** | enable/disable per account, change settings |
| **Profiles** | retained future infrastructure (records only — capabilities are direct account→plugin assignments) |
| **Rate Limits** | pacing policies, the sequential lane, effective rates |
| **Tasks** | the outbound queue, history, wait times |
| **Logs / Audit** | live log stream + every Discord interaction |
| **Simulation** | run offline scenarios in-process |

Everything the dashboard does, the CLI also does (`python3 main.py …`) and
vice versa — same API underneath, so pick whichever you like.

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
with metadata, a `Setting` manifest, lifecycle hooks and optional declared
self-commands (`COMMANDS`). See `docs/plugins/writing_plugins.rst` and
`core/plugin_base.py`.

```bash
python3 main.py plugins list               # nothing until you add one
# drop a package under plugins/ (or pass --plugins <dir>), then:
python3 main.py accounts add alpha <plugin>   # name + the plugin it runs
python3 main.py accounts plugin-add alpha <plugin>
```

The test suite exercises the framework through fixture plugins
(`tests/fixtures/plugins/probe` for the loader/manager/command registry;
`tests/fixtures/sim_plugins/soulcopy` as the full-stack simulation
workload) — those are tests-only, never discovered by a production boot.

## Everyday commands

From your owner account in Discord (mention the bot account as prefix):

```
@account status            @account plugins alpha <plugin> off
@account estop             @account help
```

From the shell:

```bash
python3 main.py accounts list|start|stop|remove
python3 main.py plugins list
python3 main.py ratelimit effective --account alpha
python3 main.py estop                  # fleet-wide halt
python3 main.py --sim pacing_respected # offline sanity check
python3 main.py --bench                # performance benchmarks
```

## Safety model

Three pacing layers compose on every outbound action — the strictest wins:

| Layer | What it governs |
|---|---|
| **Sequential send lane** | one action every 1.5s across the ENTIRE fleet |
| **Sync-group clock** | accounts in one group serialize against each other |
| **Per-account pacing** | each account's own minimum interval + jitter (default 2.0s + 0.1–0.5s random) |

Plus: a hard 1.5s floor nothing can go below, server 429s always earn
paced retries, cooldowns Mudae reports are persisted with absolute expiry,
DM channels resolve once and are cached forever, and the framework itself
makes zero self-initiated API calls — every call is a plugin action through
the pipeline above.

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
tests/unit|integration|simulation|stress   1264-test offline suite
config/                global settings, rate policies, account files
main.py                entrypoint
```

The dashboard serves a sidebar SPA (Overview, Accounts, Plugins, Profiles,
Rate Limits, Cooldowns, Tasks, Logs, Simulation, Advanced) on `http://127.0.0.1:8070`
with live WebSocket push; set `DASHBOARD_TOKEN` in `.env` to require auth.

Dependencies: Python ≥ 3.10 + `aiosqlite` + `cryptography` (see
`requirements.txt`). Live mode additionally needs the user-account client
fork `discord.py-self` (`pip install discord.py-self`), imported lazily —
simulations/tests never require it.

>>>>>>> 4703931 (hardening: round-3 omni audit — 20 investigators, 10 fix agents, adversarial re-audit)
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

### Rate scopes (three composing layers)

| Scope | Key | Governs |
|---|---|---|
| **per-task** | `(account, action)` | consecutive API calls of ONE account |
| **per-account/group** | `(group:<sync_group>, action)` | group members serialize on ONE shared clock: `A → [limit] → B → [limit] → A` |
| **server-wide lane** | fleet | every outbound action, one at a time, spaced by `prefixrate` |

All three compose (the strictest wait wins). Jitter is drawn BEFORE
admission and slept after the grant, and every spacing clock (pacing and
lane alike) is stamped at grant + jitter — the expected wire instant — so
consecutive wire sends stay ≥ the effective interval apart (jitter can
never compress the gap). Server 429s always earn at least one paced
retry; `FlowBlocked` aborts only that account's flow with a human reason.
Groups are registered automatically from each account's `sync_group`
(pure metadata — no API involved).

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
across applicable levels. Policies below **1.5s/action** are rejected
(`UnsafePolicyError`); the floor only rises. The composed result is
memoized per `(action, account, plugin)` under a config generation counter
(tuple-keyed, capped, invalidated at every mutation).

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
N-account start/stop, restart/delete churn, message bursts; loop-lag
histograms; leak verdicts from trend analysis; `--repeat` medians,
`--profile` (main-thread cProfile), `--tracemalloc` (allocation sites),
`--capture-spikes` (diagnostic snapshots); baselines + tolerance-aware
compare; DB worker telemetry.

## Management surfaces

- CLI: `accounts|credentials|plugins|profiles|ratelimit|cooldowns|tasks|simulation|logs|audit|estop|status|help|quit`
- Dashboard REST: 1:1 with ControlAPI; WebSocket pushes stats/log/audit/task
  events live
- Mention prefix (owner-only, allowlisted, self-throttled, replies paced)

All three hit the same ControlAPI — parity by construction.

## Documentation

- `knowledgebase/` — deep knowledge base. **Local-only and gitignored**;
  generate with `python3 scripts/gen_knowledgebase.py [--check]`. The
  handwritten guides (architecture, per-subsystem references, plugin API,
  configuration, security, changelog) live on disk and are not shipped.
- `docs/` — Sphinx site: `pip install --user sphinx myst-parser` then
  `python3 -m sphinx -W -b html docs docs/_build/html`
- `INTEGRATION.md` — inter-module contracts
- `HARDENING_README.md` — the hardening history (phases 1–2 + round-2)
- `scripts/bench_db.py`, `scripts/bench_hot.py` — perf harnesses
- `benchmark/README.md` — the benchmark subsystem

## Anti-flag hardening summary

One REST call per entity ever (DM/user cache in SQLite, migration 002);
no double-resolution on DM sends; stale-cache healing on TargetGone;
optional typing indicator (default off); mention-prefix management
owner-only and self-throttled; N-account IDENTIFY pacing with per-IP
budget; per-account 429 cooldowns with fleet-wide global-limit handling;
reconnect backoff with jitter and flap-escalation; event-loop-friendly
everything (queued persistence, bounded dispatch slices).
