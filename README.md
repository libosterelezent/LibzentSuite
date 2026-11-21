# LibzentSuite

A multi-account Discord automation framework designed around one principle:
**your accounts should behave like well-paced humans, not bots.**

Any number of accounts share pacing clocks, back off instantly when Discord
says so, and never make an unnecessary API call — all managed from the CLI
(a FastAPI core API + Flet UI is the planned next interface). Game-specific
logic lives in pluggable parser packs, not the core.

---

## Contents

1. [What it does](#what-it-does)
2. [Install](#install)
3. [Quick start](#quick-start)
4. [Setting up accounts](#setting-up-accounts)
5. [Workspaces](#workspaces)
6. [Plugins](#plugins)
7. [Everyday commands](#everyday-commands)
8. [Safety model](#safety-model)
9. [Troubleshooting](#troubleshooting)
10. [Reference](#reference)

---

## What it does

- **Workspace orchestration** — accounts stay independent until you put
  them in a workspace; membership shares one pacing clock AND enables
  coordinated chains: *account A does something, follow-up account B
  cleans it up — if settings/plugins allow it* (visible skips with
  reasons, never silent drops)
- **Plugin framework** — self-describing plugins (settings manifest,
  lifecycle, declared self-commands, optional `EMITS`/`FOLLOWS` chain
  declarations) discovered from a directory; the framework ships with
  NONE bundled, so `plugins/` may stay empty
- **Presets** — reusable per-plugin setting templates, applied by COPY
  with provenance (the account keeps its own values; updates are
  explicit, never silent overwrites)
- **Channels** — a three-level model (registry / account assignments /
  per-plugin bindings): bindings initialize by copy from the account's
  selection, then diverge freely
- **Admin panel** — DB-backed command authorization (per-account Discord
  user-id allowlists, framework admin list, per-account command prefix
  mention/symbol/both) replacing the `OWNER_USER_IDS` env model, with a
  one-time migration
- **Anti-detection pacing** — learned per-action defaults (measured
  zero-throttle rates) composed with your policies; strictest wins
- **Everything from the CLI** — accounts, tokens (encrypted), plugins +
  presets, workspaces + chains, rate policies, live logs, audit trail,
  simulation
- **Fully offline-testable** — a complete Discord simulator runs the whole
  stack with zero network

## Install

```bash
git clone <this repo> && cd LibzentSuite
uv sync --frozen                      # the exact locked environment (runtime + dev tooling)
cp .env.example .env                  # then edit it (see below)
chmod 600 .env                        # POSIX — see the platform note below
```

You need Git and [uv](https://docs.astral.sh/uv/) on `PATH` (the Discord client
fork is a git dependency) and Python 3.10–3.13. A runtime-only install can
also use `pip install -r requirements.txt`.

Dependency versions are pinned by `uv.lock` (the universal resolution
committed at the repo root); `requirements.txt` is a generated
pip-compatible export of it — regenerate with `uv export`, never hand-edit.
Development and CI tooling (ruff, pyright, pytest + xdist) lives in the `dev`
dependency group, which `uv sync --frozen` installs.


> **Platforms** — the project is platform-independent: no platform-only
> modules, and one suite that runs green on every OS. CI runs it on Windows
> (3.10) under bash; the local gate does the same. Seven capability-bound
> tests skip with their reason (POSIX permission bits, symlinks without
> Developer Mode). The commands below write `python3` for an ACTIVATED
> project environment — `uv sync` installs it but does not activate it,
> so run `source .venv/bin/activate` first, or prefix any command with
> `uv run --frozen` instead; that form needs no activation and is the one
> to use on Windows (where the interpreter is `python`, not `python3`).
> sync with the `UV_PYTHON` environment variable: `export UV_PYTHON=3.13`
> in a POSIX shell or Git Bash, `set UV_PYTHON=3.13` in cmd,
> `$env:UV_PYTHON = "3.13"` in PowerShell. The
> local gate `scripts/local-ci.sh` is bash — on Windows, run it from Git Bash.

`.env` needs:

```ini
OWNER_USER_IDS=123456789012345678     # YOUR Discord user id — migrated ONE TIME into the DB admin list
```

Account tokens are **not** in `.env` — they're stored encrypted in the
database (see [Setting up accounts](#setting-up-accounts)).

## Quick start

```bash
# 30-second offline tour — no Discord account needed
python3 main.py --sim-list              # list scenarios
python3 main.py --sim export_basic    # full pipeline over the simulator

# go live
python3 main.py                        # interactive REPL
python3 main.py accounts list          # one-shot command mode
python3 main.py --start-all            # boot + start every enabled account
```

> **One-time migration** — on the first boot the framework seeds the DB
> admin list from `OWNER_USER_IDS` and then IGNORES the env value for
> authorization. From that point on, who can command which account is
> managed through the admin self-commands (`adminlist`, `adminperms`,
> `adminprefix` — fail-closed). If neither the env key nor the admin
> list has ids, NOBODY can invoke commands until you add an admin there.

## Setting up accounts

1. **Add the account** — `python3 main.py accounts add myaccount`
   (name plus optionally the plugin(s) it should run; assign more later
   with `accounts plugin-add`). Put it in a **workspace** with
   `workspaces add-member <workspace> <account>`.
2. **Store its token** — `python3 main.py accounts credential
   myaccount <token>`. The token is Fernet-encrypted at rest; the key
   lives in `state/credentials.key` (back that file up — losing it
   means re-entering tokens).
3. **Start it** — `accounts start myaccount`

Put every account that shares work in the same **workspace** — that's
what makes them share a pacing clock and enables chains between them.

## Workspaces

The workspace is the ONE orchestration concept (it replaced sync groups
+ the fleet module on 2026-11-14):

```bash
python3 main.py workspaces list                       # every workspace + members
python3 main.py workspaces create grind-team "the grind"
python3 main.py workspaces add-member grind-team alpha
python3 main.py workspaces add-member grind-team bravo
python3 main.py workspaces pause grind-team           # fences new work (generation bump)
python3 main.py workspaces tasks grind-team           # the work units
```

**Chains** — the coordinated flows: a plugin on account A finishes a
task (or emits a signal), and a follow-up handler runs on account B —
gated by the eligibility policy (plugin assigned ∧ enabled ∧ the
step's settings gate ∧ a usable channel binding). A denied step is a
*visible skip with the reason recorded*, never a silent drop. Plugins
opt in by declaring `EMITS` (signal names they may emit through
`await ctx.services.emit_signal(...)`) and `FOLLOWS` (triggers +
handler methods); workspaces bind the declarations into named chains.

**Signals are durable**: emission persists before it acknowledges,
delivery is at-least-once with dead-lettering, and consumers dedupe by
identity. Chain runs execute immutable definition snapshots — editing
a chain affects future runs only.

## Plugins

The framework itself ships with **no bundled plugins** — `plugins/` can
stay empty and everything still boots (the loader simply discovers zero
plugins). Plugins are self-describing packages: a `PluginBase` subclass
with metadata, a `Setting` manifest, lifecycle hooks, optional declared
self-commands (`COMMANDS` with the `ArgSpec` argument grammar) and
optional chain declarations (`EMITS` / `FOLLOWS`).

**The plugin development guide is
[`knowledgebase/PLUGIN_SYSTEM.md`](knowledgebase/PLUGIN_SYSTEM.md)**
(the formal API reference is `knowledgebase/PLUGIN_API.md`), plus
`core/plugin_base.py` and `docs/plugins/writing_plugins.rst`.

```bash
python3 main.py plugins list               # nothing until you add one
# drop a package under plugins/ (or pass --plugins <dir>), then:
python3 main.py accounts add alpha <plugin>   # name + the plugin it runs
python3 main.py accounts plugin-add alpha <plugin>
```

The test suite exercises the framework through fixture plugins
(`tests/fixtures/plugins/probe` for the loader/manager/command registry;
`tests/fixtures/sim_plugins/exporter` as the full-stack simulation
workload) — those are tests-only, never discovered by a production
boot.

## Everyday commands

From Discord — self-command verbs on any account you're allowed to invoke
(type them with the account's prefix; default prefix comes from
`SELF_COMMAND_PREFIX`, per-account from the admin commands):

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
python3 main.py workspaces list
python3 main.py ratelimit effective message alpha
python3 main.py estop                  # framework-wide halt
python3 main.py --sim pacing_respected # offline sanity check
python3 main.py --bench                # performance benchmarks
```

Debugging a live issue? Add `--debug`: DEBUG-level logs, Discord gateway
lines (reconnect/close codes), DB worker timings, scheduler pressure.

## Safety model

Pacing composes on every outbound action — the strictest wins:

| Layer | What it governs |
|---|---|
| **Workspace clock** | members of one workspace serialize against each other |
| **Per-account pacing** | each account's own minimum interval + jitter per action |
| **Learned defaults** | with no policy stored: message 2.6 s, dm 2.6 s, reaction 2.5 s, component 3.0 s (measured zero-throttle rates — fallback, not policy) |

Plus: a hard sanity floor (1.5 s/action) nothing can go below, server
429s always earn paced retries (with per-account throttle telemetry),
cooldowns persist with absolute expiry, DM channels resolve once and
are cached forever, and the framework itself makes zero self-initiated
API calls — every call is a plugin action through the pipeline.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `missing_credential` | store the token (`accounts credential <name> <token>`) |
| `invalid_credential` | token wrong/revoked — re-enter it |
| Account keeps reconnecting | young accounts get culled by Discord; see `--debug` close codes |
| "cannot boot" | unwritable `--db` path or malformed config — the message says which |
| Boot warns about permissions | `chmod 600 state/credentials.key .env` (POSIX only — on Windows the audit is skipped; restrict the `state/` folder with file-system ACLs instead) |
| Key/DB mismatch warning | `state/credentials.key` doesn't match the stored tokens — restore the key or re-enter tokens |

---

# Reference

*Everything below is the detailed technical reference. Nothing above will
change your mental model; everything below is for working ON the framework.*

## Layout

```
core/                  framework managers + shared contracts
  api.py               ControlAPI — the ONE facade the interfaces call
  bootstrap.py         composition root (the boot flow of main.py)
  account_manager/     Account (first-class), lifecycle, send hook, cache,
                       lifecycle governor + waiter registry
  workspace/           THE orchestration: service (one writer), models,
                       transitions, policy (7-check gate), chains runtime,
                       durable signals, dispatch guard, reconciler
  plugin_manager/      discovery, per-account instances, event dispatch
  plugin_base.py       the plugin contract (PluginBase/Context/Services,
                       COMMANDS + EMITS/FOLLOWS declarations)
  settings_manager.py  ONE settings schema consumed by every surface
  rate_limit_manager/  policies (global/account/plugin/pair), pacing, 429s,
                       effective-config cache, workspace clocks
  task_scheduler/      queue + priority + cancel + retries + timeouts,
                       pressure observability
  event_bus.py         typed pub/sub: coalesced bounded dispatch
  logging_service.py   structured logging (one service for everything)
  audit.py             interaction audit trail (waits/jitter per send)
  credentials.py       ids in .env; credential_service.py — DB-encrypted tokens
  config.py            config dir loading
database/              SQLite: models, ordered migrations, repositories
                       (workspaces, presets, signals, channels + the rest)
adapters/              Discord boundary: discord/ (live, lazy import) +
                       simulator/ (complete offline world)
interfaces/            cli/ (REPL + one-shot)
plugins/               your plugins (the framework ships with none — the
                       directory may be empty; test fixtures live under
                       tests/fixtures/)
benchmark/             the benchmark subsystem (scenarios, profiling modes)
tests/unit|integration|simulation|stress   ~1600-test offline suite
config/                global settings, rate policies, account files
main.py                entrypoint
```

Dependencies: Python ≥ 3.10; direct runtime constraints live in
`pyproject.toml`, with the exact pinned resolution in `uv.lock`
(`requirements.txt` is the generated pip-compat export). Live mode
additionally needs the user-account client fork `better.py-self`
(included in the resolution), imported lazily — simulations/tests never
require a live network connection.

> **Runtime environment note** — the project is platform-independent: its

> **Runtime environment note** — the project is platform-independent: its
> whole test suite runs green on Linux and on native Windows. CI runs it
> on Windows; the local gate (`scripts/local-ci.sh`, bash) does the same.
## Architecture: who calls what

```
                    ┌─────────────────────┐
                    │   LibzentSuite      │
                    │  (core framework)   │
Events ────────────►│ Event routing       │
                    │ Account control     │
                    │ Workspace chains    │
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
                               │  open_dm / fetch_user / emit_signal)
                               ▼
                    ┌─────────────────────┐
                    │     RateLimiter     │
                    │ · per-task          │  one account's consecutive calls
                    │ · per-account/      │  shared clock per workspace
                    │   workspace         │
                    └──────────┬──────────┘
                               ▼
                          API functions
```

**LibzentSuite controls the flow; plugins control API usage; the
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
| **per-account/workspace** | `(workspace:<name>, action)` | workspace members serialize on ONE shared clock: `A → [limit] → B → [limit] → A` |

Both compose (the strictest wait wins). When no policy is stored for an
action, the learned default applies (message 2.6 s, dm 2.6 s, reaction
2.5 s, component 3.0 s — measured zero-throttle rates). Jitter is drawn
BEFORE admission and slept after the grant, and every spacing clock is
stamped at grant + jitter — the expected wire instant — so consecutive
wire sends stay ≥ the effective interval apart (jitter can never
compress the gap). Server 429s always earn at least one paced retry
(counted per account); `FlowBlocked` aborts only that account's flow
with a human reason. Workspace clocks are registered automatically from
membership (pure metadata — no API involved). Retries carry no scheduler
delay — the limiter's cooldown + interval own every wait.

## Rich event model

`adapters/model.py` converts every inbound Discord event ONCE into a frozen
project-owned DTO — embeds, components (buttons/selects), attachments,
reactions, references — plus a runtime-only `raw` escape hatch
(`message.unwrap()`). Typed bus events (`MessageReceived`, `MessageEdited`,
`MessageDeleted`, `ReactionAdded`) are additive. Actions are capabilities
on `PluginServices` (`send_message`, `send_dm`, `add_reaction`,
`click_button`) through the paced pipeline; plugins also get
framework-managed cooldowns, persistent per-plugin state, and durable
signal emission (`emit_signal`).

Plugins subscribe through a **per-account waiter registry**: a waiter for
account A structurally never runs for B's events.

## Rate policies

Four levels — `global`, `account`, `plugin`, `account_plugin` — each
`{interval: seconds, jitter: [min, max]}`. Effective pacing is the MAX
across applicable levels (and the learned per-action default when no
policy is stored). Policies below the current safety floor (1.5 s) are
rejected (`UnsafePolicyError`); the floor only rises and persists across
restarts. The composed result is memoized per `(action, account,
plugin)` under a config generation counter (tuple-keyed, capped,
invalidated at every mutation). The `prefixrate` self-command sets the
account's message interval at runtime.

## Credentials & security

Tokens: Fernet-encrypted in the DB; master key outside it
(`state/credentials.key` 0600 or `LZ_MASTER_KEY`). Boot audits permissions
on db/key/.env, tightens WAL sidecars, and runs a key/DB decrypt canary.
Permission bits are a POSIX mechanism: on Windows the audit is skipped and
the key file carries no mode-based protection, so guard `state/` with
file-system ACLs there or supply the key through `LZ_MASTER_KEY`.
Diagnostic artifacts are 0600 and value-free (audited — no token material
can reach benchmark/profile/tracemalloc output).

## Simulation & benchmarks

`adapters/simulator/` is a complete in-memory Discord world (users, channels,
DM pairs, history, a scripted responder, one-shot 429s, channel deletion).
`SimulationRunner` boots a FULL ephemeral framework per run (with its own
temp master key — never touching production state) and produces a timed
execution log, the audit trail and pass/fail checks. Scenarios:
`export_basic`, `export_multichunk`, `pacing_respected`,
`server_429_respected`, `channel_gone_aborts`, `estop_halt`
(`python3 main.py --sim-list` is the source of truth).

`benchmark/` (see `benchmark/README.md`): scenarios incl. cold boot,
N-account start/stop, restart churn, message bursts; loop-lag
histograms; leak verdicts from trend analysis; `--repeat` medians,
`--profile` (main-thread cProfile), `--tracemalloc` (allocation sites),
`--capture-spikes` (diagnostic snapshots); baselines + tolerance-aware
compare; DB worker telemetry.

## Management surfaces

- CLI: `accounts|credentials|plugins|workspaces|ratelimit|cooldowns|tasks|simulation|log|global-log|audit|estop|status|help|quit` (logs are a choice: `log <account>` for one client's tail, `global-log` for the suite's own — the console stays quiet at WARNING unless `LIBZENT_CONSOLE_LEVEL` says otherwise; `--live` on either verb switches to the live log window — any keypress returns to the CLI)
- Self-commands: the attached plugins' COG verbs ONLY (the framework
  verbs left the accounts, 2026-11-17 — framework control from chat is
  retired pending a replacement design; the CLI owns it meanwhile) —
  gated per account by the admin-panel access lists
- Mention prefix (`mention` prefix mode of the same self-command layer —
  same access gate, replies paced)

All of them hit the same ControlAPI. The FastAPI core API (next phase)
will expose the same surface over REST + WebSocket.

## Documentation

- `docs/roadmap.md` — the active plan and decision records
- `docs/architecture/architecture.rst` — the binding architecture
  contract (the implemented structure, including the 18 locked
  workspace decisions)
- `docs/plans/` — planned and in-progress contracts only (the Flet UI
  design contract, the pivot audit)
- `knowledgebase/` — deep knowledge base (handwritten guides + generated
  read-only reference; `generated/*.json` is gitignored). Start at
  `knowledgebase/README.md`; the complete surface reference is
  `knowledgebase/API_ENDPOINTS.md`.
- `docs/` — Sphinx user-facing reference: `pip install --user sphinx
  myst-parser` then `python3 -m sphinx -W -b html docs docs/_build/html`
- `docs/architecture/integration.md` — inter-module contracts
- `scripts/bench_db.py`, `scripts/bench_hot.py` — perf harnesses
- `benchmark/README.md` — the benchmark subsystem
