# AGENT GUIDE — for future coding/debugging agents

You are working in **LibzentSuite**, a multi-account Discord automation
framework (stdlib-only core; live mode needs the `discord.py-self` fork).
This page tells you where things live, what to trust, and how to work
without breaking the architecture.

---

## 1. Repo map

| Path | Contents |
|---|---|
| `main.py` | Entrypoint: argparse (`--sim`, `--dashboard`, `--headless`, `--start-all`, `--env`, `--config`, `--db`, `--plugins`, `--port`), one-shot vs REPL dispatch |
| `core/bootstrap.py` | **Composition root** — the only place that knows how managers interconnect (`build()`, see §8) |
| `core/api.py` | `ControlAPI` — the ONE facade both interfaces call (parity by construction) |
| `core/account_manager/` | Account registry/lifecycle/supervisor (`manager.py`), plugin services per account (`services.py`), Discord cache (`cache.py`) |
| `core/plugin_manager/` | Discovery (`loader.py`), per-account instances + dispatch (`manager.py`) |
| `core/plugin_base.py` | The plugin contract: `PluginBase` / `PluginContext` / `PluginServices` protocol |
| `core/profile_manager/` | Profile records — RETAINED future infrastructure (NOT the capability model; capabilities are direct account→plugin assignments in `account_plugins`) |
| `core/settings_manager.py` | ONE settings schema (defaults ← global ← account), `ACCOUNT_SETTINGS` |
| `core/rate_limit_manager/` | `engine.py` (pacing), `policies.py` (4-level model, floor), `lane.py` (GlobalSendLane), `warnings.py` (safety warnings) |
| `core/task_scheduler/` | `scheduler.py` (queue/lanes/retry), `tasks.py` (Task model) |
| `core/` (files) | `event_bus.py`, `logging_service.py`, `audit.py`, `credentials.py`, `config.py`, `errors.py`, `priority.py`, `remote_control.py` |
| `adapters/` | The Discord boundary: `base.py` (client protocol), `model.py` (rich DTOs), `discord/` (live), `simulator/` (offline world/client/runner/scenarios) |
| `database/` | `db.py` (connection + repos), `schema.py` (ordered migrations), `models.py` (row dataclasses), `repositories/` |
| `interfaces/cli/` | `commands.py` (HELP registry + strict engine), `repl.py` (REPL + one-shot) |
| `interfaces/dashboard/` | `server.py` (REST+WS), `ws.py` (RFC 6455 subset), `static/index.html` (SPA) |
| `plugins/` | `roll/` (reference plugin — copy as template), `soulcopy/` |
| `config/` | `global_settings.json`, `rate_limits.json`, optional `accounts/*.json` |
| `tests/` | `unit|integration|simulation` + fixtures (see [TESTING.md](TESTING.md)) |
| `scripts/` | `run_tests.sh`, `gen_knowledgebase.py` |
| Root docs | `README.md`, `ARCHITECTURE.md` (design principles), `INTEGRATION.md` (the binding API contract between components), `TODO + Ideas.md` (the original spec) |
| `knowledgebase/` | This documentation system (see below) |

## 2. Where the docs live

| Layer | Location | Nature |
|---|---|---|
| Architecture intent | root `ARCHITECTURE.md`, `INTEGRATION.md`, `TODO + Ideas.md` | handwritten contracts |
| Handwritten reference | `knowledgebase/*.md` (this file, [CONFIGURATION](CONFIGURATION.md), [CLI](CLI.md), [DASHBOARD](DASHBOARD.md), [TESTING](TESTING.md), [SECURITY](SECURITY.md), [GLOSSARY](GLOSSARY.md), [CHANGELOG](CHANGELOG.md)) | maintained by hand |
| Generated reference | `knowledgebase/read_only/` (`SYMBOL_INDEX.md`, `modules/<mod>.md`, `plugins/<name>.md`) + `knowledgebase/generated/*.json` | **machine-generated, READ ONLY by convention** — regenerate with `python3 scripts/gen_knowledgebase.py`; `--check` diffs the current symbols against `knowledgebase/generated/drift_baseline.json` (exit 0 clean, 2 on drift) and writes `drift_report.json` |

## 3. Public vs private API

* The **supported plugin API** is the hand-maintained `PLUGIN_API` set at the
  top of `scripts/gen_knowledgebase.py` (also flagged `plugin_api: true` in
  `generated/symbol_index.json`): `core.plugin_base.*` (base/context/
  services + lifecycle hooks + `handle_event`), the `AccountPluginServices`
  methods (`send_message`, `send_dm`, `open_dm`, `fetch_user`,
  `add_reaction`, `click_button`, `wait_for_message`, `collect_messages`,
  `publish`, `validate_channel`, `describe_channel`), `TaskScheduler.submit`,
  `core.settings_manager.Setting`, `core.priority.Priority`.
* Everything else public in `core/` is **internal core API** — it can change;
  don't build plugins on it. When you add a symbol to the supported surface,
  add it to `PLUGIN_API` and regenerate.
* The plugin contract itself: `core/plugin_base.py` (docstrings are the
  contract; `plugins/roll/plugin.py` is the reference implementation).

## 4. Finding a symbol

1. `knowledgebase/read_only/SYMBOL_INDEX.md` — per-module table (classes /
   functions / symbol counts / purpose) linking to per-module pages.
2. `knowledgebase/generated/symbol_index.json` — machine-readable inventory
   (qualified names, signatures, docstrings, tags).
3. `knowledgebase/generated/module_dependencies.json` — import graph.
4. Then **jump to the source** — the generated pages intentionally carry no
   architectural intent, only signatures.

## 5. Lifecycle pointer

Boot order lives in `core/bootstrap.build()` (docstring): Credentials →
Config → Database → LogService(+db sink) → EventBus → RateLimitManager →
SettingsManager → AuditRecorder → TaskScheduler (late-bound sender) →
PluginManager → ProfileManager → AccountManager (restores persisted
accounts) → sender bound → `scheduler.start()` → SimulationRunner →
ControlAPI (+ `COMMAND_HUB.api_provider` bridging the self-command
verbs). Shutdown reverses with logging-order care
(`Framework.shutdown`). Architectural narrative: root `ARCHITECTURE.md`
("Boot flow"), `docs/architecture/lifecycle.rst` in the Sphinx tree.

## 6. Side-effect tags are heuristics

The generator's tags (`creates_tasks`, `sleeps`, `publishes_event`,
`db_write`, `network`) come from **regex scans of function bodies**
(`_SIDE_EFFECT_PATTERNS` in `scripts/gen_knowledgebase.py`). They are
labelled heuristics, not facts — a call in a comment or a string can trip
them, and indirection can hide a real side effect. **Verify in source**
before relying on a tag for a security- or concurrency-relevant decision.

## 7. Priority order (what wins in a conflict)

1. **Source code** — the only ground truth.
2. **Tests** — executable specification of intended behaviour.
3. **Schema/config** (`database/schema.py`, `config/*.json`,
   `core/settings_manager.py` schemas).
4. **Generated reference** (`knowledgebase/read_only/`,
   `knowledgebase/generated/`) — derived, possibly stale, never intent.
5. **Handwritten docs** (this knowledgebase, root docs) — may lag.

**Record discrepancies:** when documentation (any layer) disagrees with
source, fix the doc if it's yours, and always record the mismatch in
`knowledgebase/DISCREPANCIES.md` rather than silently working around it.

## 8. Running tests + common debugging entry points

Tests (offline-only, no tokens needed):

```bash
bash scripts/run_tests.sh                     # full suite (~550 tests)
python3 -m unittest tests.unit.test_rate_lane # one module
```

Debugging entry points:

| Task | Start here |
|---|---|
| "How does X get built/wired?" | `core/bootstrap.build()` — the composition order (§5) |
| "A send is slow/queued/missing" | `AccountManager.send_hook` → `TaskScheduler` worker → `RateLimitManager.acquire()` (lane stage 0 → cooldowns → pacing → jitter) → adapter `send_message`; timing lands in `audit_logs.wait_ms/jitter_ms` |
| "Why did pacing wait?" | `RateLimitManager.acquire` + `effective()` (max across levels, floor) — reproduce with `ManualClock` + `_sleep` seam (see [TESTING.md §2](TESTING.md#2-determinism-techniques)) |
| "A plugin didn't react" | `PluginManager` dispatch → `PluginBase.handle_event` (filter-fast contract) → plugin's own trigger check (owner ids from `credentials.owner_user_ids`) |
| "Boot crashed" | `core/config.load_config` (loud failures on malformed JSON), `Credentials.require`, `Database.migrate` |
| "Dashboard misbehaves" | `interfaces/dashboard/server.py` `_route` (the single routing table), then the SPA's `showErr` accrual box |
| "Settings not applying" | `SettingsManager.resolve` layering + `PluginContext.settings` live view (in-place updates, `SettingChanged` event) |
| "What changed in the docs vs code?" | `python3 scripts/gen_knowledgebase.py --check` |

## 9. Conventions worth keeping

* Fail **loud** on malformed config (typo traps); validate at the boundary
  (NaN, strict bools, exact arg counts).
* Domain errors: precise human messages via the
  `core/errors.py` family; interfaces render them verbatim.
* Persistence is **best-effort** for pacing safety — the runtime-enforced
  policy is authoritative; a failing DB write never weakens rate limiting.
* New outbound capability → goes through the send pipeline, never a direct
  client call from a plugin.
* Appendix-only migrations (`database/schema.py`): never edit an applied
  migration.
