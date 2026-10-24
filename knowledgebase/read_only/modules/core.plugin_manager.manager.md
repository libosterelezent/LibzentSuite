# Module `core.plugin_manager.manager`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\plugin_manager\manager.py`

```
PluginManager: per-account plugin instances, lifecycle, dispatch (spec §2).

One manager owns every (account, plugin) pair in the fleet:

  * ``attach_account``   instantiate instances for an account (profile-filtered),
  * ``activate_account`` resolve settings + fire on_load/on_enable (async —
                         lifecycle ordering is awaited, not fire-and-forget),
  * ``deactivate_account`` fire on_disable/on_unload (async, error-isolated),
  * ``set_enabled``      persist-first runtime toggling with lifecycle hooks,
  * ``set_setting``      validated setting changes with a live settings view,
  * ``states``           per-plugin runtime state incl. error counters.

CONTRACT NOTE (deviation, documented): INTEGRATION.md sketched
activate/deactivate as sync, but plugin lifecycle hooks are async — awaiting
them requires async methods. They are async here; the account manager calls
them from its own async start/stop flows.

Error isolation is absolute: a failing hook or handler disables/records THAT
plugin only — the ported discipline from three audit rounds.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `PluginRuntime` | dataclass | yes | One plugin instance bound to one account (manager-internal). |
| `PluginManager` | class | yes | Discovery + lifecycle + event dispatch for every account's plugins. |
| `_ManagerSettingsApi` | class | no | CommandSettingsApi over the plugin manager's own surfaces. |

### `PluginManager`

- `def discover(self) -> Dict[str, Any]`
  - Scan the plugin dir (raises PluginError on inconsistency).
- `async def persist_metadata(self, repo: Any=None) -> int` — side effects: `db_write`
  - Persist the discovered plugins' metadata rows (boot path).
- `def manifests(self) -> List[Dict[str, Any]]`
  - JSON-safe manifests (metadata + settings schema) for both UIs.
- `def names(self) -> List[str]`
- `async def attach_account(self, account: str, allowed: Optional[Set[str]]=None) -> None`
  - Instantiate this account's plugins (profile allowlist applies).
- `async def reload_plugin(self, name: str) -> Dict[str, Any]`
  - Hot-reload one plugin: validate → teardown → restore LIVE runtimes.
- `async def unload_plugin(self, name: str) -> Dict[str, Any]`
  - Unload one plugin: tear down on every account, unregister UI.
- `def detach_account(self, account: str) -> None`
  - Drop the account's instances WITHOUT firing unload hooks.
- `async def activate_account(self, account: str, services: Any) -> None`
  - Resolve settings + fire on_load/on_enable for enabled plugins.
- `def init_boot_dashboard_uis(self) -> int`
  - Register plugin dashboard UIs at DISCOVERY time (no account).
- `def active_instance_for(self, plugin_name: str, account=None)`
  - VALIDATED live instance for a plugin, optionally per account.
- `def services_for(self, account: str, plugin_name: str)`
  - The account's services facade for one plugin (or None).
- `def close(self) -> None`
  - Tear the manager down: bus subscriptions off, pending tasks reaped.
- `async def deactivate_account(self, account: str) -> None`
  - Fire on_disable + on_unload for the account's active plugins.
- `def ui_registry(self) -> Any`
  - The plugin dashboard-UI registry (Stage D routes dispatch here).
- `async def set_enabled(self, account: str, plugin: str, enabled: bool) -> None` — side effects: `publishes_event, db_write`
  - Toggle a plugin at runtime (persisted BEFORE memory mutates).
- `async def set_setting(self, account: str, plugin: str, key: str, value: Any) -> Any` — side effects: `db_write`
  - Validate + persist + apply one plugin setting for one account.
- `async def refresh_settings(self, account: Optional[str]=None, plugin: Optional[str]=None) -> int`
  - Re-resolve the LIVE settings of attached runtimes (in place).
- `async def clear_setting(self, account: str, plugin: str, key: str) -> Any`
  - Remove one account-layer override; live view falls back to the
- `async def ui_stats(self, account: str) -> List[Dict[str, Any]]`
  - Collect every enabled plugin's Home-tab stat cards (JSON-safe).
- `async def ui_panels(self) -> List[Dict[str, Any]]`
  - Collect every plugin's sidebar panels (JSON-safe, deduped by id).
- `def states(self, account: str) -> List[Dict[str, Any]]`
  - Per-account plugin state for both UIs (sorted by name).
- `def dispatch_stats(self, account: str) -> Dict[str, int]`
  - Bounded-dispatch health for one account: how many dispatch
- `def allowed_names(self, account: str) -> List[str]`
  - Names currently attached to the account (for error messages).
- `def live_settings(self, account: str, plugin: str) -> Optional[Dict[str, Any]]`
  - The LIVE resolved-settings dict for one (account, plugin) pair.
- `def command_entries(self, account: str) -> List[Tuple[CommandSpec, str]]`
  - ``(CommandSpec, plugin name)`` pairs for the account's plugins.
- `def commands_for(self, account: str) -> List[CommandSpec]`
  - The account's plugin-declared CommandSpecs (bound handlers,
- `async def execute_self_command(self, account: str, name: str, args: List[str], channel_id: int, *, guild_id: Optional[int]=None, invoker_id: Optional[int]=None, raw: str='') -> None`
  - Run one self-command and reply in-channel through the pipeline.

### `_ManagerSettingsApi`

- `def schema(self) -> tuple`
- `async def binding(self) -> Optional[str]`
- `async def profiles(self) -> List[Dict[str, Any]]`
- `async def effective(self) -> Dict[str, Any]`
  - Fully merged effective settings for the (account, plugin).
- `async def set(self, key: str, value: Any) -> Any` — side effects: `db_write`
  - Same validation path as /api/plugins/set-setting.
- `async def clear(self, key: str) -> Any`
