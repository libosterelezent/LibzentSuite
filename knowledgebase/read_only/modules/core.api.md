# Module `core.api`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\api.py`

```
CoreAPI — the ONE application surface both interfaces call (spec §13/§14).

CLI and dashboard are thin wrappers over these methods and NOTHING else, so
they can never drift apart in capability (parity by construction). This
facade is intentionally duck-typed over the managers: it stores whatever it
is given and delegates. Managers are constructed by core/bootstrap.py.

Method map (grouped like the dashboard navigation, spec §12):
  status            overview: accounts (+credential status, cooldown count)
                    + limits + tasks + warnings
  accounts_*        list/add/remove/info/enable/start/stop/emergency-stop
                    (+ account_setting_get/account_setting_set for the
                    framework per-account knobs, and
                    accounts_plugins_list/plugin_assign/plugin_unassign/
                    plugins_set — the DIRECT account→plugin capability
                    assignments, persisted through db.account_plugins;
                    accounts_plugin_profile(s) bind an ASSIGNED plugin
                    to a settings profile, accounts_effective_settings
                    merges defaults <- profile <- account overrides, and
                    accounts_channels snapshots the live client's text
                    channels)
  plugin_profiles_* list/create/update/delete of per-plugin setting
                    profiles (db.plugin_profiles, migration v7) with
                    settings validated against the plugin's Setting
                    schema and the reserved "channels" scope against
                    core/plugin_scopes
  credentials       set_credential / credential_status / import_env over the
                    encrypted CredentialService (values NEVER echoed back)
  cooldowns_*       list/clear persistent cooldowns (CooldownService)
  plugins_*         manifests/state/enable/settings/set-setting
                    (set-setting value null = plugins_clear_setting: the
                    account-layer override is DELETED, unmasking the lower
                    layers — not pinned to the schema default)
                      infrastructure — bookkeeping only, NO effect on which
                    plugins an account runs; capability assignment lives
                    in accounts_plugin_*)
  groups_*          sync groups: list/get/create/update/delete + membership
                    (add/remove account, set/clear account) + snapshot;
                    membership persists through SyncGroupManager and the
                    runtime pacing scope through RateLimitManager
  ratelimit_*       policies/set/clear/effective/stats/warnings/set-floor
                    (+lane; every mutation persists through the db repos)
  tasks_*           list/cancel/cancel-matching/history
  simulation_*      list/run
  logs_tail / audit_tail
  emergency_stop_all / shutdown

Management audit: account, credential and emergency-stop mutations emit an
audit row via the recorder (``action="mgmt:*"``, ``direction="out"``, note
naming the calling surface); other management calls currently do not.
Credential material NEVER reaches the audit trail — account names and
counters only.

Manager migration note: the managers are moving to aiosqlite-backed async
persistence; call sites below tolerate BOTH sync and async manager results
through ``_resolved()`` so this facade keeps working at every step.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `ControlAPI` | class | yes | High-level operations over every manager (JSON-safe in/out). |

### `ControlAPI`

- `async def status(self) -> Dict[str, Any]`
  - Overview snapshot: accounts + rate limits + tasks + warnings.
- `async def accounts_list(self) -> List[Dict[str, Any]]`
  - Every account: name, discord id (when running), status,
- `async def accounts_add(self, payload: Dict[str, Any], surface: str='api') -> Dict[str, Any]`
  - Register + persist an account; optionally store its credential.
- `async def accounts_remove(self, name: str, surface: str='api') -> Dict[str, Any]`
  - Remove a STOPPED account from runtime + database (cascade).
- `async def accounts_info(self, name: str) -> Dict[str, Any]`
  - Account details page (spec §8): status/identity/runtime/plugins/rate.
- `async def accounts_enable(self, name: str, enabled: bool) -> Dict[str, Any]`
- `async def accounts_start(self, name: Optional[str]=None, surface: str='api') -> Dict[str, Any]`
  - Start one account (None = every enabled account).
- `async def accounts_stop(self, name: Optional[str]=None, surface: str='api') -> Dict[str, Any]`
  - Stop one account (None = all) — graceful.
- `async def accounts_emergency_stop(self, name: str, surface: str='api') -> Dict[str, Any]`
  - Account-scoped emergency stop: cancel its tasks NOW + disconnect.
- `async def accounts_plugins_list(self, account: str) -> List[str]`
  - Sorted plugin names assigned to the account.
- `async def accounts_plugin_assign(self, account: str, plugin: str, surface: str='api') -> Dict[str, Any]`
  - Assign ONE plugin to an account (idempotent; re-attaches NOW).
- `async def accounts_plugin_unassign(self, account: str, plugin: str, surface: str='api') -> Dict[str, Any]`
  - Remove ONE plugin assignment (idempotent; re-attaches NOW).
- `async def accounts_plugins_set(self, account: str, plugins: List[str], surface: str='api') -> Dict[str, Any]`
  - REPLACE the account's whole assignment set (re-attaches NOW).
- `async def plugin_profiles_list(self, plugin: Optional[str]=None) -> List[Dict[str, Any]]`
  - Every profile (or one plugin's), plugin-then-name order.
- `async def plugin_profile_create(self, plugin: str, name: str, description: str='', settings: Optional[Dict[str, Any]]=None, surface: str='api') -> Dict[str, Any]`
  - Create one plugin profile (settings validated against schema).
- `async def plugin_profile_update(self, plugin: str, name: str, *, name_new: Optional[str]=None, description: Optional[str]=None, settings: Optional[Dict[str, Any]]=None, surface: str='api') -> Dict[str, Any]`
  - Update name/description/settings of one profile.
- `async def plugin_profile_delete(self, plugin: str, name: str, surface: str='api') -> Dict[str, Any]`
  - Delete one profile; its account bindings cascade to NULL.
- `async def accounts_plugin_profile(self, account: str, plugin: str, profile: Optional[str], surface: str='api') -> Dict[str, Any]`
  - Bind (or clear: profile=None) the account's profile for a plugin.
- `async def accounts_plugin_profiles(self, account: str) -> Dict[str, Optional[str]]`
  - ``{plugin: profile name or None}`` for every ASSIGNED plugin.
- `async def accounts_effective_settings(self, account: str, plugin: str) -> Dict[str, Any]`
  - Effective settings for one (account, plugin), fully merged.
- `async def accounts_channels(self, account: str) -> List[Dict[str, Any]]`
  - The account's visible TEXT channels, from the live client cache.
- `async def account_setting_get(self, name: str) -> Dict[str, Any]`
  - Resolved framework account settings (reconnect policy...).
- `async def account_setting_set(self, name: str, key: str, value: Any, surface: str='api') -> Dict[str, Any]`
  - Set ONE framework account setting (validated + persisted).
- `async def accounts_set_credential(self, name: str, credential: str, surface: str='api') -> Dict[str, Any]`
  - Store (replace) one account's credential — encrypt-at-rest only.
- `async def accounts_credential_status(self) -> List[Dict[str, Any]]`
  - [{name, credential}] for every account — display-safe, no values.
- `async def credentials_import_env(self, overwrite: bool=False, surface: str='api') -> Dict[str, Any]`
  - MIGRATION-ONLY: explicit one-shot import of legacy .env tokens.
- `async def cooldowns_list(self, account: Optional[str]=None) -> List[Dict[str, Any]]`
  - Active persistent cooldowns of one account (None = every account).
- `async def cooldowns_clear(self, account: str, plugin: str, cooldown_key: Optional[str]=None, surface: str='api') -> Dict[str, Any]`
  - Clear ONE cooldown (``cooldown_key`` given) or ALL of a plugin.
- `async def plugins_manifests(self) -> List[Dict[str, Any]]`
  - Installed plugins incl. their settings schema (auto-generated UIs).
- `async def plugins_state(self, account: str) -> List[Dict[str, Any]]`
  - Per-account plugin runtime state incl. resolved settings + errors.
- `async def plugins_reload(self, plugin: str, surface: str='api') -> Dict[str, Any]`
  - Hot-reload one plugin (detach → re-import → re-attach).
- `async def plugins_unload(self, plugin: str, surface: str='api') -> Dict[str, Any]`
  - Unload a plugin (detach everywhere, unregister UI; assignments
- `async def plugins_set_enabled(self, account: str, plugin: str, enabled: bool) -> Dict[str, Any]`
  - Enable/disable a plugin on an account (lifecycle hooks fire).
- `async def plugins_settings(self, account: str) -> List[Dict[str, Any]]`
  - Resolved settings per plugin for one account.
- `async def plugins_set_setting(self, account: str, plugin: str, key: str, value: Any) -> Dict[str, Any]` — side effects: `db_write`
  - Change ONE plugin setting for ONE account (validated + persisted).
- `async def plugins_clear_setting(self, account: str, plugin: str, key: str) -> Dict[str, Any]`
  - Reset ONE plugin setting: DELETE the account-layer override.
- `async def groups_list(self) -> List[Dict[str, Any]]`
  - Every sync group with its member count (sorted by name).
- `async def groups_get(self, group: Any) -> Dict[str, Any]`
  - One group by id or name (SyncGroupError when unknown).
- `async def groups_create(self, payload: Dict[str, Any], surface: str='api') -> Dict[str, Any]`
  - Create a group (may have zero members).
- `async def groups_update(self, group: Any, payload: Dict[str, Any], surface: str='api') -> Dict[str, Any]`
  - Update name/description/enabled; a rename moves every member's
- `async def groups_delete(self, group: Any, detach_members: bool=False, surface: str='api') -> Dict[str, Any]`
  - Delete a group (accounts are NEVER deleted). Refuses while
- `async def groups_add_account(self, group: Any, account: str, surface: str='api') -> Dict[str, Any]`
  - Put an account in a group (replaces any previous membership).
- `async def groups_remove_account(self, group: Any, account: str, surface: str='api') -> Dict[str, Any]`
  - Remove an account from one group (back to per-account pacing).
- `async def groups_set_account(self, account: str, group: Any, surface: str='api') -> Dict[str, Any]`
  - Set/replace an account's group (None -> G and G1 -> G2).
- `async def groups_clear_account(self, account: str, surface: str='api') -> Dict[str, Any]`
  - Remove the account from any group (group -> None).
- `async def groups_snapshot(self, group: Any) -> Dict[str, Any]`
  - Group snapshot: identity/metadata + members + runtime pacing
- `async def ui_stats(self, account: str) -> List[Dict[str, Any]]`
  - Per-account Home-tab stat cards from every enabled plugin.
- `async def ui_panels(self) -> List[Dict[str, Any]]`
  - Plugin-contributed sidebar panels (framework-level).
- `async def dashboard_ui_navigation(self) -> List[Dict[str, Any]]`
  - Plugin pages for the sidebar, VISIBILITY-CHECKED.
- `async def dashboard_ui_page(self, plugin: str, page_id: str, account: Optional[str]=None) -> Optional[Dict[str, Any]]`
  - One page's full serialized component tree; None when the
- `async def dashboard_ui_action(self, plugin: str, component_id: str, payload: Dict[str, Any], surface: str='api', account: Optional[str]=None) -> Dict[str, Any]`
  - Dispatch a registered component action (visibility-checked).
- `async def dashboard_ui_data(self, plugin: str, component_id: str, account: Optional[str]=None) -> Any`
  - Fetch a data-provider result (visibility-checked, JSON-safe).
- `async def admin_overview(self) -> Dict[str, Any]`
  - The admin panel's full view: admin ids, mudae bot id, and every
- `async def admin_set_ids(self, ids: List[int], surface: str='api') -> Dict[str, Any]`
- `async def admin_add_id(self, user_id: int, surface: str='api') -> Dict[str, Any]`
- `async def admin_remove_id(self, user_id: int, surface: str='api') -> Dict[str, Any]`
- `async def admin_account_flag(self, account: str, is_admin: bool, surface: str='api') -> Dict[str, Any]`
- `async def admin_perms(self, account: str, surface: str='api') -> Dict[str, Any]`
  - Read the per-account command-permission list.
- `async def admin_perms_set(self, account: str, ids: List[int], surface: str='api') -> Dict[str, Any]`
- `async def admin_perms_add(self, account: str, user_id: int, surface: str='api') -> Dict[str, Any]`
- `async def admin_perms_remove(self, account: str, user_id: int, surface: str='api') -> Dict[str, Any]`
- `async def admin_prefix(self, account: str, surface: str='api') -> Dict[str, Any]`
  - Read the per-account self-command prefix configuration.
- `async def admin_prefix_set(self, account: str, mode: str, symbol: str='', surface: str='api') -> Dict[str, Any]`
- `async def admin_mudae_id(self, surface: str='api') -> Dict[str, Any]`
  - Read the framework-level Mudae bot id.
- `async def admin_mudae_id_set(self, user_id, surface: str='api') -> Dict[str, Any]`
- `async def admin_forget_account(self, account: str, surface: str='api') -> Dict[str, Any]`
  - Remove an account's access row (admin/perms/prefix) — cleanup
- `async def ratelimit_policies(self) -> List[Dict[str, Any]]`
  - Every stored policy with its level/owner scope.
- `async def ratelimit_set(self, action: str, interval: float, jitter_min: float=0.0, jitter_max: float=0.0, level: str='global', owner: Optional[str]=None) -> Dict[str, Any]` — side effects: `db_write`
  - Set a policy. Enforces the safety floor: an interval at or below the
- `async def ratelimit_clear(self, level: str, owner: Optional[str], action: str) -> Dict[str, Any]`
- `async def ratelimit_effective(self, action: str, account: Optional[str]=None, plugin: Optional[str]=None) -> Dict[str, Any]`
  - What actually paces this (account, plugin, action) right now.
- `async def ratelimit_stats(self) -> Dict[str, Any]`
  - Visualization data: policies, cooldowns, last fires, counters.
- `async def ratelimit_warnings(self) -> List[Dict[str, Any]]`
  - Safety review of the current configuration.
- `async def ratelimit_set_floor(self, seconds: float) -> Dict[str, Any]`
  - Raise the pacing floor (never below MIN_ACTION_INTERVAL; persisted).
- `async def ratelimit_lane(self) -> Dict[str, Any]`
  - Sequential send lane state (REMOVED — returns stub for compat).
- `async def ratelimit_set_lane(self, enabled: Any=None, prefixrate: Any=None) -> Dict[str, Any]`
  - Lane configuration (REMOVED — accepts and returns stub for compat).
- `async def tasks_list(self, status: Optional[str]=None, account: Optional[str]=None) -> List[Dict[str, Any]]`
  - Queued/running tasks (and recent ones) with optional filters.
- `async def tasks_cancel(self, task_id: int) -> Dict[str, Any]`
- `async def tasks_cancel_matching(self, account: Optional[str]=None, plugin: Optional[str]=None) -> Dict[str, Any]`
  - Emergency-stop helper: cancel every matching task.
- `async def tasks_history(self, limit: int=50, account: Optional[str]=None) -> List[Dict[str, Any]]`
- `async def simulation_list(self) -> List[Dict[str, Any]]`
- `async def simulation_run(self, name: str) -> Dict[str, Any]`
  - Run one scenario offline; returns the full timed report.
- `async def logs_tail(self, n: int=100, account: Optional[str]=None, severity: Optional[str]=None) -> List[Dict[str, Any]]`
  - Structured log tail (dashboard Logs page + CLI 'logs').
- `def logs_service(self) -> 'LogService'`
  - The live service (dashboard WS subscribes to this).
- `def audit_recorder(self) -> Optional['AuditRecorder']`
  - The live audit recorder (dashboard WS subscription point).
- `async def audit_tail(self, n: int=100, account: Optional[str]=None) -> List[Dict[str, Any]]`
- `def dashboard_token(self) -> Optional[str]`
- `async def emergency_stop_all(self, surface: str='api') -> Dict[str, Any]`
  - GLOBAL emergency stop: cancel all tasks, stop all accounts, halt.
- `async def system_resume(self) -> Dict[str, Any]`
  - Resume after a GLOBAL emergency stop.
- `async def shutdown(self) -> Dict[str, Any]`
  - Graceful shutdown of the scheduler + accounts (quit/dashboard).

## Functions

- `def parse_bool(value: Any, field: str) -> bool`
  - Strict boolean parsing shared by every mutation surface.
