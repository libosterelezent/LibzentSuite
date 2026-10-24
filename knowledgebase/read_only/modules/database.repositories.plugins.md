# Module `database.repositories.plugins`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `database\repositories\plugins.py`

```
PluginRepository — plugin manifests, per-account states and settings.

Three tables, one facade:

* ``plugins``          discovered-plugin metadata (manifest for the UI);
* ``plugin_states``    per-account enabled/disabled flag (absent = default);
* ``plugin_settings``  per-account setting overrides (JSON values).

Metadata is returned as plain dicts shaped like PluginMetadata.to_dict():
``{"name", "version", "description", "settings"}`` — the UI/settings layers
consume exactly that shape.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `PluginRepository` | class | yes | Manifest + state + settings persistence for plugins, keyed by NAME. |

### `PluginRepository`

- `async def upsert_metadata(self, name: str, version: str, description: str, settings_manifest: Any) -> None` — side effects: `db_write`
  - Insert or refresh a discovered plugin's manifest row.
- `async def metadata(self) -> List[Dict[str, Any]]`
  - All plugin manifests as PluginMetadata-shaped dicts (name order).
- `async def metadata_for(self, name: str) -> Optional[Dict[str, Any]]`
  - One plugin's manifest dict (None when the plugin is unknown).
- `async def state(self, account: str, plugin: str) -> Optional[bool]`
  - Persisted enabled flag for (account, plugin).
- `async def set_state(self, account: str, plugin: str, enabled: bool) -> None` — side effects: `db_write`
  - Persist the enabled flag (upsert); survives restarts.
- `async def setting(self, account: str, plugin: str, key: str) -> Any`
  - One stored setting value (None when absent).
- `async def settings(self, account: str, plugin: str) -> Dict[str, Any]`
  - All stored overrides for one (account, plugin) ({} when none).
- `async def set_setting(self, account: str, plugin: str, key: str, value: Any) -> None` — side effects: `db_write`
  - Store one per-account setting override (upsert, JSON value).
- `async def delete_setting(self, account: str, plugin: str, key: str) -> bool`
  - Remove one per-account setting override; True when a row existed.
- `async def all_account_settings(self) -> Dict[str, Dict[str, Dict[str, Any]]]`
  - The whole override layer: account -> plugin -> key -> value.
