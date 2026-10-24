# The Plugin System

> Overview of discovery, lifecycle, settings, event routing and the capability model.
> The **formal API reference** lives in [PLUGIN_API.md](PLUGIN_API.md); worked examples in
> [PLUGIN_EXAMPLES.md](PLUGIN_EXAMPLES.md). Additional API details are documented in the source modules and generated symbol reference.
> Generated declarations: [`read_only/plugins/`](read_only/plugins/) and
> [`read_only/modules/core.plugin_base.md`](read_only/modules/core.plugin_base.md).

Source of truth for this page: `core/plugin_base.py`, `core/plugin_manager/loader.py`,
`core/plugin_manager/manager.py`, `core/plugin_manager/commands.py`,
`core/plugin_scopes.py`, `core/settings_manager.py`,
`core/account_manager/{manager,services}.py`. The framework ships with
NO plugins — the working examples live in `tests/fixtures/plugins/probe`
(the loader/manager/command-registry/UI-extension fixture),
`tests/fixtures/plugins/config_demo` (the dashboard-UI reference) and
`tests/fixtures/sim_plugins/soulcopy` (the simulation workload).

---

## 1. Discovery (`core.plugin_manager.loader.PluginLoader`)

One `PluginLoader` owns one directory (production default: `plugins/`). `discover()` rescans the
whole tree on every call — each scan builds a **fresh synthetic parent package**
(`_mudae_plugroot_<n>`, a `types.ModuleType` whose `__path__` is the plugins dir and which is
registered in `sys.modules`), so package plugins get working relative imports
(`from .textutils import ...`) with **no `sys.path` hacks** and no collision with real packages.
Production normally scans once at boot (`PluginManager.discover()`).

### 1.1 Layout conventions (relative to the plugins dir)

| Layout | Kind | Notes |
|---|---|---|
| `<dir>/<name>/__init__.py` | **package plugin** (preferred) | submodules may use relative imports |
| `<dir>/<name>.py` | single-file plugin | loaded via `importlib.util.spec_from_file_location` under a unique synthetic name |

Entries starting with `_` or `.` are skipped. Directories whose name contains a `.` are skipped
with a warning (they cannot be imported as packages).

A discovered module is scanned for classes that (a) subclass `core.plugin_base.PluginBase`,
(b) are not `PluginBase` itself, and (c) were **defined in that module or its submodules**
(checked via `__module__` prefix). A module exporting no such class logs a warning
("did you forget to inherit?") instead of being silently skipped.

### 1.2 Name rules (hard errors)

* Plugin slugs must fully match `[a-z0-9_-]+` (`loader._NAME_RE`). A missing, non-string or
  non-matching `name` raises `core.errors.PluginError`.
* Rationale kept from the audit: `str.isalnum()` accepted Unicode slugs that could never
  round-trip through settings keys.
* **Duplicate plugin names** are a hard `PluginError` (both sources named).
* **Duplicate setting keys** inside one plugin's `settings` manifest are a hard `PluginError`.
* Any import/execution failure of a plugin module raises `PluginError` with the path and cause.

### 1.3 The record and accessors

```python
@dataclass(frozen=True)
class PluginRecord:          # core/plugin_manager/loader.py
    name: str                # the slug
    cls: Type[PluginBase]    # the discovered class
    source: Path             # where it came from
```

* `PluginLoader.discover() -> Dict[str, PluginRecord]`
* `PluginLoader.names() -> List[str]` (lazy discover, sorted)
* `PluginLoader.record(name) -> PluginRecord` (`PluginError` when unknown)
* `PluginLoader.create(name) -> PluginBase` — a **fresh instance**; the plugin manager calls this
  once per (account, plugin) pair
* `PluginLoader.manifests() -> List[dict]` — JSON-safe manifests (see §2)

`PluginManager.discover()` additionally upserts plugin metadata rows into the database
(`PluginRepository.upsert_metadata`) so the Plugins page works before any account attaches;
persistence failures are logged, never fatal.

---

## 2. Manifest schema

`PluginLoader.manifests()` (surfaced by `PluginManager.manifests()` → `ControlAPI.plugins_manifests()`)
returns one JSON-safe dict per plugin, sorted by name:

```python
{
    "name": "probe",
    "version": "1.1.0",
    "description": "test fixture",
    "settings": [Setting.as_manifest(), ...],
}
```

`core.settings_manager.Setting.as_manifest()`:

```python
{"name": str, "type": "str"|"int"|"float"|"bool", "default": Any,
 "description": str, "min": float|None, "max": float|None}
```

The **same** manifest auto-generates the settings editors on both surfaces (CLI `set` validation and
the dashboard Plugins page) — parity by construction, no per-surface implementations.

---

## 3. Per-(account, plugin) instantiation

The unit of execution is **one plugin instance bound to one account**. There is never a shared
plugin object across accounts.

Flow (driven by `core.account_manager.AccountManager`, bookkept by
`core.plugin_manager.manager.PluginManager`):

1. **`PluginManager.attach_account(account, allowed=None)`** — instantiate this account's plugins.
   `allowed` is the account's DIRECT assignment set (§7): the account manager passes
   `Account.assigned_plugins` (the `account_plugins` rows); `None` keeps every discovered
   plugin, otherwise only names in the set survive. For each survivor a `PluginRuntime` is created
   (`loader.create(name)`), with its persisted enabled state read from
   `PluginRepository.state(account, name)` (`None` → default **enabled**).
2. **Settings resolve at attach too** (not only at activate) so bad config fails fast: a
   `SettingsManager.resolve` failure **disables the plugin** and records
   `last_error = "settings: <ExcType>: <msg>"` with `error_count += 1`.
3. `attach` happens at boot (account restored from the repository), on `add_account`, on every
   `start()` — and IMMEDIATELY after any assignment mutation
   (`assign_plugin`/`unassign_plugin`/`set_account_plugins` re-attach right
   away; a running account's plugin set follows the change with no restart).
4. `PluginManager.detach_account(account)` drops the instances **without** firing unload hooks
   (the account manager calls `deactivate_account` first when the plugins were active).

`PluginRuntime` (manager-internal): `name`, `instance`, `enabled`, `loaded`, `active`,
`settings` (the live dict), `last_error`, `error_count`.

---

## 4. Lifecycle

All hooks are `async` and are **awaited** (ordering is real, not fire-and-forget). The account
manager calls these from its own async start/stop flows (documented deviation from INTEGRATION.md,
which sketched them sync — see the `core/plugin_manager/manager.py` docstring).

### 4.1 Activate — `PluginManager.activate_account(account, services)`

`services` is either a `PluginServices`-like object **or** a factory `(plugin_name) -> services`
(duck-checked: callable without a `send_message` attribute). The account manager passes
`AccountManager._services_factory(name)` so each plugin gets its own
`core.account_manager.services.AccountPluginServices` facade.

For each **enabled** runtime (`_activate_one`):

1. Resolve settings again (`SettingsManager.resolve(plugin, schema, account=account)`); failure →
   disable + record error + return.
2. Build a `PluginContext` (account, plugin, the live settings dict, the per-plugin services
   facade, logger `libzent.<account>.<plugin>`).
3. `on_load(ctx)` — once ever per attach cycle (`loaded` flag), then `on_enable(ctx)`.
4. `runtime.active = True`; publish `PluginLoaded(account=..., plugin=...)` on the bus.

Any hook failure is recorded on the runtime (`last_error = "<hook>: <ExcType>: <msg>"`,
`error_count += 1`), the plugin is **disabled**, and other plugins continue activating.

### 4.2 Deactivate — `PluginManager.deactivate_account(account)`

For every runtime: `on_disable(ctx)` (only when `active and enabled`), then `on_unload(ctx)`
(only when `loaded`), each error-isolated (recorded, swallowed — unload always runs), then
`active = False`, `loaded = False` and `PluginUnloaded` published. Hook contexts still see the
cache because the account manager deactivates plugins **before** disconnecting the client.

### 4.3 Runtime toggle — `PluginManager.set_enabled(account, plugin, enabled)`

**Persist-first**: `PluginRepository.set_state` writes the flag *before* memory mutates, so a crash
mid-toggle cannot resurrect a stale state.

* enabling (was disabled, account active) → `_activate_one` (settings + `on_load`/`on_enable`).
* disabling (was enabled, was active) → `on_disable` only (deliberately **not** `on_unload`).
* publishes `PluginEnabledChanged(account, plugin, enabled)`.

### 4.4 The hooks (contract, `core.plugin_base.PluginBase`)

| Hook | When | Idempotency expectation |
|---|---|---|
| `on_load(ctx)` | first time the plugin is enabled on an account | do one-time setup |
| `on_enable(ctx)` | every time the plugin becomes active (after `on_load`) | cheap, repeatable |
| `on_disable(ctx)` | disabled or the account stops | release transient resources |
| `on_unload(ctx)` | removed from the account (last call) | final cleanup |

All hooks have default no-op implementations; `handle_event` likewise. Failing clean and quiet is
the plugin's job (one abort line); per-account isolation is the dispatcher's.

---

## 5. Event routing

### 5.1 Which events reach `handle_event`

`PluginManager.__init__` subscribes on the bus to exactly **four account-scoped Discord event
types**:

| Event (`core.event_bus`) | Fields beyond `Event.account`/`Event.ts` |
|---|---|
| `MessageReceived` | `message_id`, `channel_id`, `author_id`, `content`, `dm`, `message` (rich DTO, may be `None` for synthetic events) |
| `MessageEdited` | `message_id`, `channel_id`, `guild_id`, `message` (rich post-edit DTO) |
| `MessageDeleted` | `message_id`, `channel_id`, `guild_id` (ids only) |
| `ReactionAdded` | `message_id`, `channel_id`, `guild_id`, `user_id`, `emoji`, `dm`, `raw` (runtime-only, `repr=False`) |

New event types are **additive**: plugins filter with `isinstance` inside `handle_event`.
Framework events (`TaskCompleted`, `SettingChanged`, `PluginEnabledChanged`, `AccountConnected`,
scenario events, …) are **not** routed to plugins. Plugins that want their own event vocabulary
may define `core.event_bus.Event` subclasses and `services.publish(...)` them.

### 5.2 Dispatch mechanics (`PluginManager._on_message` → `_dispatch`)

* The bus handler is sync: it creates **one fire-and-forget `asyncio.Task` per event** (strong refs
  in `_pending`) so a slow plugin can never block the publisher (the client callback → bus path).
* The task looks up the runtimes of `event.account` (no account → no dispatch).
* Every runtime with `enabled and active` gets a **freshly built `PluginContext`** and is awaited
  **sequentially** (dict insertion order = attach order). A slow prologue in `handle_event` is
  multiplied by the plugin count — filter fast (see `PluginBase.handle_event`'s docstring).
* **Per-plugin error isolation**: any exception (except `asyncio.CancelledError`, which
  propagates) increments `runtime.error_count`, sets
  `runtime.last_error = "<EventType>: <ExcType>: <msg>"`, logs the traceback once via the
  manager logger — and the remaining plugins still run. This means a flow abort re-raised from `handle_event` is recorded as a plugin error by the dispatcher, rather than treated as a clean flow termination.

### 5.3 Surfaced state — `PluginManager.states(account)`

```python
[{"name": ..., "enabled": bool, "loaded": bool, "settings": dict-copy,
  "last_error": str|None, "error_count": int}, ...]   # sorted by name
```

Both UIs read this (`ControlAPI.plugins_state`); `Account.plugins` caches the
`enabled`/`last_error`/`error_count` projection for account status snapshots.

---

## 6. Settings

### 6.1 The `Setting` schema (`core.settings_manager.Setting`, frozen dataclass)

| Field | Meaning |
|---|---|
| `name` | simple identifier (must pass `name.replace("_","").isalnum()`), unique within the plugin |
| `type` | one of `("str", "int", "float", "bool")` |
| `default` | validated at construction (a bad default fails at import time) |
| `description` | human text shown in the auto-generated editors |
| `min_value` / `max_value` | numeric bounds (**only** for `int`/`float`) |

`Setting.validate(value)` coerces + bounds-checks: bools accept `"1/true/yes/on"` /
`"0/false/no/off"` (case-insensitive); ints reject fractional floats; floats reject NaN/inf;
everything out of bounds raises `SettingsValidationError(name, reason)`.

### 6.2 Three-layer resolution (`SettingsManager.resolve`)

```
effective = defaults  <-  global layer  <-  account layer
```

1. **defaults** — validated schema defaults (lowest priority),
2. **global layer** — `config/global_settings.json` (`plugin -> {key: raw}`),
3. **account layer** — database `plugin_settings` rows (`account -> plugin -> {key: raw}`).

Unknown keys and bad values raise `SettingsValidationError` **naming the layer** — boot fails
fast; runtime changes surface as one clean error line. Resolution runs at attach (fail-fast) and
again at activate.

### 6.3 Changing a setting — `PluginManager.set_setting(account, plugin, key, value)`

Validates against the plugin's schema (`SettingsManager.set_plugin_setting`: validate → persist to
the account layer → emit `SettingChanged`), then **mutates the live dict in place**
(`runtime.settings[key] = validated`). The same dict object is shared by every `PluginContext`
and by `services.settings` (wired via `PluginManager.live_settings` /
`AccountManager.services_for`), so:

* `ctx.settings` is a **live view** — a long-lived plugin instance sees setting changes without
  being rebuilt;
* `states()["settings"]` is a **copy** for display;
* `live_settings(account, plugin)` hands out the real dict (or `None` when the pair is unknown).

Both surfaces edit through the same path: CLI `set` → `ControlAPI.plugins_set_setting`; dashboard
`POST /api/plugins/set-setting` — the manifest generates the editor, the schema validates the value.

### 6.4 Framework account settings (not plugin settings)

`core.settings_manager.ACCOUNT_SETTINGS` (`reconnect_enabled`, `reconnect_initial_delay`,
`reconnect_max_delay`, `reconnect_multiplier`, `typing_probability`) are per-account framework
knobs resolved by `SettingsManager.account_settings` — see [ACCOUNT_SYSTEM.md](ACCOUNT_SYSTEM.md).

---

## 7. Capability model (direct assignment) & profiles

**Current (implemented): direct `Account → Plugins` assignment.** Which
plugins attach to an account is decided solely by the `account_plugins`
table rows (`Account.assigned_plugins` is the in-memory mirror;
`AccountManager._allowed_plugins(name) -> set` — NEVER `None`, an empty
set attaches ZERO plugins, fail-closed; the old "no profiles =
unrestricted" default is gone). Assignment mutations persist then
re-attach immediately (§3); assignment ≠ enabled (the per-account
enabled flag is a separate `plugin_states` toggle).

**Plugin setting profiles (implemented).** Each ASSIGNED plugin may be
bound to one named settings **profile** (`plugin_profiles` table; the
binding column lives on the assignment row, `account_plugins.profile`,
`NULL` = plugin defaults). The effective settings merge is
**defaults ← bound profile ← per-account overrides** (the profile's
reserved `"channels"` key is the channel scope, not a setting):
`core.plugin_scopes.validate` / `channel_allowed` own that structure
(`{"mode": "all"|"allow"|"deny", "rules": [{"guild_id", "channel_ids"}]}`
— ids are STRINGS; an empty allow-list allows NOTHING). Every surface
(`ControlAPI.accounts_effective_settings`,
`CommandSettingsApi.effective()`, the attach-time resolve) sees the
same merge. Full model: [ACCOUNT_SYSTEM.md §4.3](ACCOUNT_SYSTEM.md).

The legacy ProfileManager was REMOVED (2026-10-22): capabilities are DIRECT account→plugin assignments (`account_plugins`); per-plugin settings presets live in `plugin_profiles` (v7).

**dashboard pages** with the builder-based Dashboard UI API — one
optional SYNC hook, `initialize_dashboard_ui(self, ui)`, assembles a
validated component tree (pages → tabs → sections → cards → inputs,
buttons, forms, tables, metrics) through the fluent
`DashboardUIToolset` builders and registers it with
`self.attach_dashboard_ui(layout)`:

* **Actions are direct Python callables** (`set_action(self._cb)`,
  signature `(ActionContext, payload) → ActionResult`) — no manual
  registration; dispatch is visibility-checked, error-isolated and
  normalized server-side by `core.dashboard_ui.registry.DashboardUIRegistry`.
* **Data providers** (`set_data_source`) feed tables/list-views with
  JSON-safe rows; **visibility checks** (`set_check`, on every
  component) are fail-closed and applied to rendered definitions AND
  server-side requests; **`set_disabled` is UI state, not
  authorization**.
* **IDs**: automatic `uuid4().hex[:12]`, or explicit
  `[A-Za-z0-9_-]{1,64}`; duplicates are rejected at add time AND at
  tree validation (`DuplicateComponentIdError` /
  `TreeValidationError`) — failed mutations never corrupt the tree.
* **Lifecycle**: attach validates + freezes the tree (mutations raise
  `FrozenLayoutError`); updates are explicit (build fresh → re-attach);
  reload/unload detaches stale registrations; UI-init failures never
  fail the account activation.
* **Scope**: one UI per PLUGIN (first account to activate wins) — not
  per account; per-account behavior lives in the callbacks
  (`context.account`) and providers.

Served by 4 routes (`/api/dashboard-ui/navigation|page|action|data`)
and rendered by the generic component renderer
(`interfaces/dashboard/static/js/dashboard-ui.js`).

**Full author's guide (every builder, forms + field_errors, data
shapes, lifecycle, scope): [DASHBOARD_UI_API.md](DASHBOARD_UI_API.md).**
Working reference example:
`tests/fixtures/plugins/config_demo/plugin.py` (every pattern, driven
end-to-end by `tests/unit/test_dashboard_ui_acceptance.py`).

---

## 8. What a plugin actually touches at runtime

```
PluginContext (one per (account, plugin), built per hook/dispatch)
├── account: str
├── plugin: str
├── settings: Dict[str, Any]        # LIVE resolved view (mutated in place by set_setting)
├── services: PluginServices        # per-(account, plugin) facade — the ONLY I/O surface
└── logger: logging.Logger          # libzent.<account>.<plugin>
```

Everything plugins may depend on is specified in [PLUGIN_API.md](PLUGIN_API.md) (stability tiers:
public plugin API vs internal core API vs private). The framework ships
with NO bundled plugins — the loader discovers whatever you drop into
`plugins/` (see [plugins/index.md](plugins/index.md) for how to add one;
the shipped examples are test fixtures under `tests/fixtures/`).
