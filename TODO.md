# TODO — Discord Selfbot Framework

Project: Modular selfbot framework for Mudae automation with multi-account support,
centralized rate limiting, and dual management interfaces (GUI dashboard + terminal).

> Reference: `Soulcopy/autocopy.py` was migrated into `plugins/soulcopy/` (see
> `knowledgebase/KB_10_SOULCOPY_MIGRATION.md`).

---

## Phase 0 — Project Scaffolding  ✅ DONE

- [x] Initialize project structure (`core/ plugins/ cli/ dashboard/ config/ tests/ knowledgebase/`)
- [x] Choose dependencies (stdlib-only core; live Discord fork lazily imported)
- [x] Define config file format(s) (global + per-account + per-plugin settings schema, validated)

## Phase 1 — Plugin Architecture (Selfbot Framework)  ✅ DONE

- [x] Define plugin/module API (`PluginBase`: name, version, description)
- [x] Plugin manifest: declare supported settings (`SettingDef`, typed + bounded)
- [x] Settings system: 3-layer resolve (defaults < global < account) with fail-fast validation
- [x] Plugin loader: discover/import package or single-file plugins
- [x] Event dispatch: route Discord events to plugins (subclass-aware bus)
- [x] Plugin lifecycle hooks (on_load, on_enable, on_disable, on_unload)
- [x] Error isolation: one plugin crashing must not take down the framework (tested)
- [x] Per-plugin and per-account logging (`mudae.<account>.<plugin>`)
- [x] Migrate `Soulcopy/autocopy.py` into the plugin system (ids moved to .env)

## Phase 2 — Multi-Account Container  ✅ DONE

- [x] Account model: credentials, status, per-account settings overrides
- [x] Central container: spawn/stop/restart individual account sessions
- [x] Credential storage in `.env` (gitignored, chmod 600; no plaintext in repo)
- [x] Sync groups (`broadcast`) + independent mode (account×plugin matrix per config)
- [x] Shared context: plugins coordinate via container helpers (collect_messages/wait_for/broadcast)
- [x] Health reporting: per-account status + last_error; graceful failure containment
- [ ] Auto-reconnect with exponential backoff on dropped sessions (pending)

## Phase 3 — Centralized Rate Limiter  ✅ DONE

- [x] Global limiter service shared by all accounts (two-layer: global + per-account buckets)
- [x] Per-bot limits: global default overridable per account (`set-limit [account]`)
- [x] Precedence rules: explicit per-bot setting > global default (tested)
- [x] Limit dimensions: `message` and `dm` actions (extensible per action key)
- [x] Priority queues: HIGH/NORMAL/LOW (tested HIGH beats LOW)
- [x] Jitter/randomization to avoid rigid timing patterns (bounded, seeded in tests)
- [x] Live view: current usage, queue depth, per-account stats (`stats()` / `limits`)
- [x] Integration point: ALL outbound sends pass through the limiter (enforced in AccountSession)

## Phase 4 — Management Interfaces

### Terminal (CLI/TUI)  ✅ DONE
- [x] Interactive terminal REPL over the shared ControlAPI (injectable stdio, testable)
- [x] Commands: status, plugins, start/stop, enable/disable, limits, set-limit, env, quit
- [ ] Live log tailing in a full-screen TUI (pending)

### GUI Dashboard  🔶 BACKEND DONE
- [x] Backend JSON API (REST) exposing the same control surface as the terminal
- [x] Endpoints: status, plugins, limits, start/stop, plugin toggle, limit set/clear
- [x] Local token auth (`DASHBOARD_TOKEN` + `X-Dashboard-Token`)
- [ ] Browser frontend pages (Accounts, Plugins, Rate limits, live logs) — pending
- [ ] Real-time WebSocket updates — pending

### Shared
- [x] Single ControlAPI layer used by both interfaces (parity by construction)

## Phase 5 — Hardening & Docs

- [x] Graceful shutdown: terminal quit + stop_all disable plugins and disconnect cleanly
- [ ] State persistence across restarts (enabled accounts / plugin toggles)
- [x] README: setup, adding an account, running sims/tests
- [x] Knowledgebase: function-level docs (`knowledgebase/`, read-only, gitignored)
- [x] Test suite: 82 tests incl. local simulations (`bash scripts/run_tests.sh`)

---

## Notes / Risks

- ⚠️ Selfbots violate Discord ToS — risk of account suspension. Keep the rate limiter
  conservative and document the risk for every account added.
- Tokens/ids only in `.env` (gitignored, chmod 600); tests use synthetic ids.
- `knowledgebase/` is local-only and read-only on disk (`chmod -R a-w knowledgebase/`).
