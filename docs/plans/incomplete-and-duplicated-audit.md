# LibzentSuite — Incomplete Functionality + Duplicated Code Audit

**Date:** 2026-11-13 · **Base:** `da61ce9` (post dashboard removal) · **Suite:** 1,673 green · **Ruff:** clean · **Mypy:** ~1,086 advisory

---

## 1. Incomplete functionality

### 1.1 Dead code from the dashboard removal (should be deleted)

| Item | Location | Status |
|---|---|---|
| `dashboard = None` variable | `main.py:84` | Orphaned — always None, the `if dashboard is not None` guard in the finally block is dead |
| `dashboard.stop()` guard | `main.py:147-151` | Dead — the variable can never be non-None |
| `init_boot_dashboard_uis()` method | `core/plugin_manager/manager.py:763` | Returns 0 always — still called at line 438 (reload path) |
| `self._ui_registry` attribute | `core/plugin_manager/manager.py:170` | Always None in production — the guard code (`if self._ui_registry is not None`) is dead |
| `--headless` help text | `main.py` | Says "use with --dashboard" but --dashboard no longer exists |

### 1.2 Retired code still in the tree (only simulator uses it)

| Item | Location | Lines | Justification to keep/remove |
|---|---|---|---|
| `core/remote_control.py` | `RemoteControl` class | 365 | Only referenced by `adapters/simulator/runner.py:492` and `tests/unit/test_round3_reaudit_b.py:445`. The self-command layer replaced it in production. **Keep only if the simulator scenario (`control_mention`) is still needed; otherwise delete.** |

### 1.3 Declared but not yet used (architecture placeholders — intentional)

| Item | Location | What exists | What's missing |
|---|---|---|---|
| `FleetTaskHandler` capability | `core/fleet/capability.py` | Full protocol: `prepare/execute/suspend/recover` + `declare()` for task definitions. 33 fleet tests. | **Zero plugins use it** — no `fleet_handler` in any production or fixture plugin. It's a valid extension point, default OFF by design (Phase 9 §5). |
| Plugin data rollup (`metric_hints`) | `core/fleet/models.py:304` | `MetricHint` dataclass + `metric_hints` field on `TaskDefinition`. | **No rollup aggregation code** — the model declares the intent but nothing consumes the hints for data rollup. The MEMORY section's local view is the shipped approach; rollup is opt-in future work. |
| Parser registry | `parsers/manager.py` | Full `ParserManager` with registration, dispatch, catalog, lazy caching. 12 tests. | **Zero parser packs registered** — the registry ships empty. The pattern is documented and tested; game packs plug in through `register_parser()`. |

### 1.4 v2 architecture — design-only, zero backend (from ARCHITECTURE.md §1-13)

| Concept | Status | What exists |
|---|---|---|
| Workspaces | Design only | `ARCHITECTURE.md` §2 defines them; no `Workspace` model, no DB table, no `workspace_id` anywhere in code |
| Collections | Design only | No `Collection` class, no DB table; the only "Collection" reference is a docstring in `plugin_manager/manager.py:1229` about a different concept |
| Execution Plans | Design only | `ARCHITECTURE.md` §4 defines them; no model, no table, no service |
| Schedules | Design only | No cron/schedule model beyond the TaskScheduler's immediate dispatch |
| Workspace-scoped configuration | Design only | Current settings are global → account (no workspace layer) |
| Scope selector UI | Design only | The scope hierarchy (Global → Workspace → Account) has no backend representation |

### 1.5 Partially implemented (works but incomplete)

| Feature | What works | What's missing |
|---|---|---|
| Plugin hot-reload | `reload_plugin()` re-imports, re-attaches, re-activates on all accounts | The `ui_registry` re-registration step is a no-op (registry is None); stale docstring still mentions UI takeover |
| Fleet reconciliation | 30s idempotent passes, zombie fencing, orphan release, ghost-group surfacing | No operator-facing resolution for surfaced ambiguities (the reconciler logs warnings but the Fleet Hub is gone — the REST surface was deleted with the dashboard) |
| Theme system | Theme persistence (`state/theme.json`), presets, accent/radius/density/fonts, custom CSS — all in `ControlAPI` | **No UI consumes it** — the SPA is deleted; the theme API methods remain on the facade with no consumer |
| Account cache snapshot | Runtime cache with guild/channel/user snapshots, persisted to `api_cache` table | Dashboard surface gone — the Accounts page that displayed it was deleted |
| `--debug` mode | DEBUG logging, discord gateway INFO, DB worker telemetry | Dashboard Overview page that surfaced it is gone; the CLI `status` still works |

---

## 2. Duplicated code

### 2.1 Near-duplicate validation (channel-scope snowflake check)

**Files:** `core/api.py:847-855` vs `database/plugin_data.py:124-131`

| | `api.py::_data_scope` | `plugin_data.py::_validate_scope` |
|---|---|---|
| Checks | `value == "0" or value.isdigit()` | `value == PLUGIN_GLOBAL or value.isdigit()` |
| Exception | `ValueError` | `PluginError` |
| Message | `field '{name}' must be a digit string or '0'` | `plugin-data {name} must be a digit string or '0'` |

Same invariant, same validation logic, different exception types and message prefixes. **Not merged** because they're at different boundaries (API input vs store write) with different error vocabularies — merging would couple the API layer to the store's error type.

### 2.2 Parallel settings-resolution paths

**Files:** `core/settings_manager.py:279` (`resolve()`) vs `core/plugin_manager/manager.py:1753` (`effective()`)

Both walk the same four-layer merge (defaults → global → profile → account override), but:
- `settings_manager.resolve()` is the canonical resolution used at attach/activate time
- `plugin_manager.effective()` is the display view (reads from repos, doesn't mutate runtime state, includes the reserved `channels` scope)

The duplication is **intentional per the docstring**: "Same order SettingsManager.resolve enforces... this manager-side view is the four-layer one." They serve different consumers (activation vs display). Not a defect.

### 2.3 Repeated `contextlib.suppress` pattern (28+ instances)

| File | Count |
|---|---|
| `core/account_manager/manager.py` | 6 |
| `core/plugin_manager/manager.py` | 5 |
| `adapters/simulator/runner.py` | 4 |
| `core/credential_service.py` | 3 |
| `core/account_manager/services.py` | 3 |
| `adapters/discord/client.py` | 3 |
| Others | 4+ |

Not exact duplication — each suppresses different exception types for different isolation boundaries (teardown, best-effort notifications, cache learning). AGENTS.md §2 explicitly warns against "abstractions created solely to eliminate a few harmless repeated lines." **Not a defect.**

### 2.4 Zero exact-duplicate function bodies detected

The AST-level hash comparison found **no exact duplicate function bodies** across the entire `core/` tree. Code re-use is through shared methods and imports, not copy-paste.

---

## 3. Summary of actionable cleanup (ordered by impact)

| # | Item | Action | Effort |
|---|---|---|---|
| 1 | `main.py` orphaned `dashboard` variable + `finally` block | Delete variable + dead guard | 5 min |
| 2 | `--headless` help text | Update: remove "--dashboard" reference | 2 min |
| 3 | `init_boot_dashboard_uis()` in plugin_manager | Delete method + its call at line 438 | 5 min |
| 4 | `self._ui_registry` attribute + guards in plugin_manager | Remove attribute, remove `if self._ui_registry is not None` guards, remove `ui_registry` parameter | 10 min |
| 5 | Theme API methods on ControlAPI | Remove (no consumer) OR keep for FastAPI migration — **decision needed** | 5-15 min |
| 6 | `core/remote_control.py` (365 lines) | Delete if the simulator `control_mention` scenario is no longer needed | 10 min |
| 7 | Stale reload docstring (mentions UI takeover) | Update comment | 2 min |

**Not actionable (intentional architecture):**
- FleetTaskHandler, MetricHint, ParserManager — declared extension points, tested, not yet consumed
- v2 workspaces/collections/plans/schedules — design-only by the v2 architecture document
- Settings resolution paths — intentional parallel (canonical vs display)
- `contextlib.suppress` repetition — isolation boundaries, not duplication
