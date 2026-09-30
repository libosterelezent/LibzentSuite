# SlaughterMudae — Architecture

The component structure (user spec §1) and how the two run modes (user spec
§2/§3) plug in. Module-level detail lives in `knowledgebase/` (KB_00 index).

```
Automation Framework
├── Account Manager      core/container.py (AccountContainer) + core/account.py
├── Plugin Manager       core/plugin_loader.py + core/plugin_base.py + plugins/*
├── Profile Manager      core/profiles.py (ProfileManager — safety presets)
├── Task Scheduler       core/scheduler.py (TaskScheduler — interval jobs)
├── Rate Limit Manager   core/rate_limit_manager.py facade over:
│                          core/rate_limiter.py (token buckets + delays)
│                          core/rest_buckets.py (Discord's own headers, 429s)
│                          core/send_engine.py (serialized lane, prefixrate)
├── Event Bus            core/events.py
├── Settings Manager     core/settings.py + core/config.py
├── Database             core/database.py (SQLite, 0600)
└── Logging              core/logging_setup.py (+ RingBufferHandler tail)

Supporting components:
├── Credentials          core/credentials.py (tokens+ids ONLY in .env)
├── Discord Cache        core/discord_cache.py (real-time, invalidation)
├── REST Buckets         core/rest_buckets.py (header-aware per-route/guild)
├── Errors               core/errors.py (FlowBlocked contract)
├── Control API          core/api.py (the ONE shared CLI/dashboard surface)
├── Dashboard            dashboard/ (SPA + REST + WebSocket)
├── Terminal/TUI         cli/
└── Simulator            core/simulator.py (in-memory Discord world)
```

Named component facades (`core/account_manager.py`,
`core/plugin_manager.py`, `core/profile_manager.py`,
`core/task_scheduler.py`, `core/rate_limit_manager.py`,
`core/event_bus.py`, `core/settings_manager.py`,
`core/database_manager.py`, `core/logging_manager.py`) give each block a
stable import path for future features.

## Live mode (§2)

```
Automation Framework
        │
        ▼
Discord Adapter        core/discord_adapter.py (lazy import, header intake,
        │              single-flight fetches, cache ingestion via cached_snapshot)
        ▼
User-account Client    e.g. discord.py-self (lazily imported fork)
```

Real-time rules: the cache is populated from what the client already holds
at READY (zero fetches); sends validate against the cache first; the only
fetches go through the account's single-flight REST gate; Discord's own
rate-limit headers are fed back into the RestBucketManager.

## Testing (§3)

```
Automation Framework
        │
        ▼
Simulation Adapter     core/simulator.py (FakeWorld/FakeClient + scripted
                       Mudae responder) — same production code path, zero
                       network, synthetic ids only
```

- `tests/` — 310+ offline tests (unit + e2e + fuzz-derived regressions)
- `scripts/sim_inspect.py` — audited interaction report
- `tests_live/live_suite.py` — OPT-IN real-Discord suite (env-gated; read-only
  by default, one message max with --send, 1.5s minimum lane spacing)

## Boot flow (live)

```
.env ──► Credentials ──► load_config ──► Database (restore: rules/delays/
         engine/profiles/jobs) ──► SettingsStore ──► RateLimiterService ──►
         RoleRegistry ──► AuditRecorder ──► AccountContainer(+SendPolicy) ──►
         add accounts (config + DB) ──► ControlAPI ──► {Terminal | TUI | Dashboard}
```

Every mutation (settings, rules, delays, roles, jobs, engine) goes through
`ControlAPI`, so the terminal and the dashboard can never diverge.
