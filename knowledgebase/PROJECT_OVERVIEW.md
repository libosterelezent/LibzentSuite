# LibzentSuite — Project Overview

This file is the entry point of the knowledgebase: it explains what the
LibzentSuite project *is*, the surfaces you manage it through, the safety
philosophy that shapes every design decision, the verified quickstart
commands, and the repository layout. Deeper material lives in
[ARCHITECTURE.md](ARCHITECTURE.md) (component topology and the API boundary
contract), [COMPONENT_MAP.md](COMPONENT_MAP.md) (per-component reference),
[DATA_FLOW.md](DATA_FLOW.md) (inbound/outbound/management flows), and
[RUNTIME_LIFECYCLE.md](RUNTIME_LIFECYCLE.md) (boot, account lifecycle,
shutdown, reconfiguration). Everything below was verified against source at
commit `35176a82` (see `knowledgebase/read_only/SYMBOL_INDEX.md`).

## What the project is

LibzentSuite is a **multi-account Discord automation framework** for the
Mudae collector bot, written in Python ≥ 3.10 with a standard-library-only
core (`requirements.txt`: zero hard dependencies). Its defining idea, stated
in `README.md` and `INTEGRATION.md` and enforced throughout the source, is a
strict division of labor:

* **The framework** (`core/`) does event routing, account control, task
  orchestration and lifecycle — and *never* initiates a Discord API call for
  its own purposes (the complete, audited client-touch inventory lives in the
  module docstring of `core/account_manager/manager.py`; see
  [ARCHITECTURE.md](ARCHITECTURE.md#the-api-boundary-contract)).
* **Plugins** (`plugins/`) decide *when* an API call is actually required and
  request it through the `core.plugin_base.PluginServices` protocol — they
  never touch a client, the rate limiter, or the database.
* **The rate limiter** (`core.rate_limit_manager.RateLimitManager`) decides
  *when those calls may execute* — pacing, jitter, fleet serialization and
  server-provided 429 cooldowns.

State persists in SQLite (`database/`, repositories + ordered migrations).
The Discord boundary is an adapter pair (`adapters/`): `RealDiscordClient`
over a lazily imported user-account client fork (`discord.py-self`), and
`SimClient` over a complete in-memory world — which is what makes the whole
framework fully runnable offline (`python3 main.py --sim ...`, 550 test
functions, no network, no tokens).

## The management surfaces (parity by construction)

All three surfaces call **one** facade — `core.api.ControlAPI` — so they can
never drift apart in capability:

| Surface | Module | Notes |
|---|---|---|
| CLI (REPL) | `interfaces.cli.repl.Repl` | prompt `sm> `, `help` for commands; `api.shutdown()` runs on every exit path |
| CLI (one-shot) | `interfaces.cli.repl.run_once` + `interfaces.cli.commands.run_command` | `python3 main.py <command ...>`; lifecycle owned by `main.py` |
| Browser dashboard | `interfaces.dashboard.server.DashboardServer` | stdlib `ThreadingHTTPServer` + hand-rolled RFC 6455 WebSocket (`interfaces.dashboard.ws`); SPA at `interfaces/dashboard/static/index.html`; JSON REST maps 1:1 to ControlAPI |
| Discord chat commands | `core/plugin_manager/commands.py` (self-command layer) | users allowed per account (or admins) type the account's prefix (mention/symbol/both, Admin tab) + verb; gated by `AccessManager.can_invoke` (fail-closed), replies go through the SAME paced send pipeline. The retired `core.remote_control.RemoteControl` survives only for the simulator's `control_mention` scenario (`REMOTE_CONTROL` gates that simulator behavior only) |

The command vocabulary (`interfaces.cli.commands.HELP`):
`status · accounts · credentials · plugins · profiles · groups ·
ratelimit · cooldowns · tasks · simulation · logs · audit ·
estop [resume] · help · quit`.

## Safety philosophy

Selfbot usage violates Discord's Terms of Service; the project's stated
position (README warning banner) is that its safety features **mitigate** risk
without authorizing it. Concretely, in source:

1. **The 1.5 s/action hard floor.** `core.rate_limit_manager.policies.MIN_ACTION_INTERVAL = 1.5`.
   No policy faster than the floor can ever be *set* — `RateLimitManager._store`
   raises `core.errors.UnsafePolicyError` — and `effective()` additionally
   takes `max(interval, floor)` so even a hand-edited database row can never
   pace faster than the floor. `RateLimitManager.set_floor` is **raise-only**
   (lowering it raises `RatePolicyError`).
2. **Human-like jitter.** Every policy carries a `jitter_min..jitter_max`
   window; `RateLimitManager.acquire` draws one uniform sample *after* the
   grant and sleeps it (it never moves the pacing clock). Advisory warnings
   (`core.rate_limit_manager.warnings.evaluate_policies`) flag missing or
   narrow (< 0.3 s spread) jitter as robotic.
3. **The sequential send lane.** `core.rate_limit_manager.lane.GlobalSendLane`
   — a server-wide admission gate: when enabled, every outbound action from
   *every* account is admitted one at a time, spaced by `prefixrate` seconds
   (`prefixrate=0` **pauses** the lane: all outbound traffic holds until a
   non-zero rate is set). The shipped `config/rate_limits.json` enables it
   with `prefixrate: 1.5`. It complements, not replaces, per-account pacing.
4. **Server limits are always respected.** Adapters translate Discord 429s
   into `core.errors.ServerRateLimit(retry_after, global_)`;
   `TaskScheduler` reports them to the limiter (fleet-wide or per
   `(account, action)` cooldown via `RateLimitManager.report_server_limit`)
   and a 429 **always earns at least one retry** (`retry_budget =
   max(max_retries, 1)` in `TaskScheduler._run_task`).
5. **Concurrency = 1 per account.** `TaskScheduler` runs one serialized worker
   lane per account — never two in-flight Discord requests for one account at
   the same moment (a classic selfbot flag vector, per the scheduler
   docstring).
6. **API-call hygiene.** One REST call per entity *ever*: DM channels and
   users resolve memory cache → the per-account `api_cache` table (migration
   002) → a single client call recorded in both. DM sends reuse the resolved
   channel id. The single deliberate framework-initiated client touch — a
   typing indicator (`AccountManager._maybe_typing`, anti-flag: humans
   trigger typing) — ships **disabled** (`typing_probability` account setting
   defaults to `0.0`).

## Quickstart (verified against `main.py` argparse)

`main.main()` defines exactly these flags: `--sim [NAME]`, `--sim-list`,
`--config DIR` (default `config`), `--plugins DIR` (default `plugins`),
`--env FILE` (default `.env`), `--start-all`, `--dashboard`, `--headless`,
`--port N` (default `8070`), `--db FILE` (default `state/libzent.db`);
unknown trailing arguments become a one-shot CLI command line. Note: the
module docstring also advertises a reserved `--tui` flag that argparse does
**not** define (see the discrepancy list in the session report).

```bash
cp .env.example .env                  # fill MUDAE_BOT_ID, OWNER_USER_IDS, DISCORD_TOKEN_<NAME>; chmod 600
python3 main.py --sim-list            # list offline scenarios (no network)
python3 main.py --sim soulcopy_basic  # run one scenario on a full ephemeral stack
python3 main.py                       # interactive REPL (accounts restored from DB)
python3 main.py accounts list         # one-shot command mode (any CLI command)
python3 main.py --start-all           # boot every ENABLED account, then REPL
python3 main.py --start-all --dashboard --port 8070   # live + dashboard + WS
python3 main.py --dashboard --headless                # daemon mode (Ctrl-C to stop)
bash scripts/run_tests.sh             # = python3 -m unittest discover -s tests -t . -v
```

Registered scenarios (`adapters.simulator.scenarios.SCENARIOS`):
`soulcopy_basic`, `soulcopy_multichunk`, `pacing_respected`,
`server_429_respected`, `channel_gone_aborts`, `control_mention`
(README lists only the first five — see discrepancy list).

Credentials (`.env`, gitignored): `MUDAE_BOT_ID`, `OWNER_USER_IDS`,
one `DISCORD_TOKEN_<ACCOUNT_NAME_UPPER>` per account, optional
`DASHBOARD_TOKEN`, optional `REMOTE_CONTROL` (default on; `0` disables).
Token values never enter the database — accounts persist only the `.env` key
reference (`database.models.AccountRecord.token_ref`).

## Repository layout (verified with `ls`)

```
LibzentSuite/
├── main.py                     entrypoint (argparse + asyncio.run + shutdown)
├── core/                       framework managers + shared contracts
│   ├── api.py                  ControlAPI — the ONE facade both interfaces call
│   ├── bootstrap.py            composition root: build() + Framework.shutdown()
│   ├── plugin_base.py          PluginBase / PluginContext / PluginServices protocol
│   ├── credentials.py          .env parsing; tokens/ids never in code
│   ├── config.py               config/ dir loading (global settings, policies, accounts)
│   ├── errors.py               domain error vocabulary (FlowBlocked family, ServerRateLimit…)
│   ├── priority.py             Priority enum shared by limiter + scheduler
│   ├── event_bus.py            EventBus + the typed event vocabulary
│   ├── logging_service.py      LogService, ServiceLogger, StdlibBridge
│   ├── audit.py                AuditRecorder (in/out interaction trail)
│   ├── remote_control.py       mention-prefix management from Discord
│   ├── account_manager/        Account, AccountRuntime, AccountManager, DiscordCache,
│   │                           AccountPluginServices + TaskAckRegistry, resolve_dm_channel
│   ├── plugin_manager/         PluginLoader (discovery) + PluginManager (lifecycle/dispatch)
│   │                           NOT the capability model)
│   ├── settings_manager.py     Setting schema + layered SettingsManager
│   ├── rate_limit_manager/     policies.py, engine.py (RateLimitManager), lane.py, warnings.py
│   └── task_scheduler/         tasks.py (Task model) + scheduler.py (TaskScheduler)
├── adapters/                   the Discord boundary
│   ├── base.py                 DiscordClient protocol (+ model re-exports)
│   ├── model.py                rich frozen DTOs + convert_message (the boundary)
│   ├── discord/client.py       RealDiscordClient + RealClientFactory (lazy discord.py-self)
│   └── simulator/              world.py (SimWorld), client.py (SimClient),
│                               scenarios.py, runner.py (SimulationRunner)
├── database/                   db.py (Database), models.py, schema.py (MIGRATIONS),
│   └── repositories/           account_plugins (direct assignments = capabilities),
│                               accounts, base, cache, kv, logs (audit+logs),
│                               plugins, profiles (retained infra), rate_policies,
│                               settings, tasks
├── interfaces/
│   ├── cli/                    commands.py (shared engine), repl.py (REPL + run_once)
│   └── dashboard/              server.py (DashboardServer), ws.py, static/index.html
├── plugins/                    roll/ (reference plugin), soulcopy/ (+ textutils.py)
├── config/                     global_settings.json, rate_limits.json (accounts/ dir optional)
├── tests/                      unit/ (25 files), integration/, simulation/, fixtures/
├── scripts/                    run_tests.sh, gen_knowledgebase.py
├── knowledgebase/              generated inventory + this documentation
├── README.md / ARCHITECTURE.md / INTEGRATION.md / TODO + Ideas.md
├── requirements.txt            (zero hard deps; optional discord.py-self note)
└── .env.example
```

## Where to go next

* How the pieces connect and the audited client-touch inventory:
  [ARCHITECTURE.md](ARCHITECTURE.md).
* One section per component with state/events/persistence/isolation:
  [COMPONENT_MAP.md](COMPONENT_MAP.md).
* Message-in, send-out and management traffic, step by step:
  [DATA_FLOW.md](DATA_FLOW.md).
* Boot order, account lifecycle, shutdown and live reconfiguration:
  [RUNTIME_LIFECYCLE.md](RUNTIME_LIFECYCLE.md).
