# Module `database.repositories.plugin_profiles`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `database/repositories/plugin_profiles.py`

```
PluginProfilesRepository — per-plugin named setting profiles (v7).

One row in ``plugin_profiles`` is a reusable bundle of setting values
for ONE plugin, keyed ``(plugin, name)``. Accounts bind to a profile
through ``account_plugins.profile`` (the binding column added by the
same migration); this repository owns the profile ROWS plus the
delete-cascade that keeps bindings from dangling.

This layer is persistence ONLY: it does NOT validate that the plugin is
discovered, and it does NOT schema-validate ``settings`` — both are the
ControlAPI's job (the API checks keys/types against the plugin's
Setting schema and the reserved "channels" structure against
core/plugin_scopes). Anything JSON-serializable stores and reads back
verbatim.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `PluginProfilesRepository` | class | yes | CRUD for plugin setting profiles, keyed by (plugin, name). |

### `PluginProfilesRepository`

- `async def create(self, plugin: str, name: str, description: str='', settings: Optional[Dict[str, Any]]=None) -> PluginProfileRecord`
  - Insert one profile; returns its persisted record.
- `async def get(self, plugin: str, name: str) -> Optional[PluginProfileRecord]`
  - One profile by (plugin, name); None when unknown.
- `async def list(self, plugin: Optional[str]=None) -> List[PluginProfileRecord]`
  - All profiles (or one plugin's), ordered by plugin then name.
- `async def update(self, plugin: str, name: str, *, name_new: Optional[str]=None, description: Optional[str]=None, settings: Optional[Dict[str, Any]]=None) -> Optional[PluginProfileRecord]`
  - Update ONLY the given fields (None = keep); bumps updated_at.
- `async def delete(self, plugin: str, name: str) -> bool`
  - Delete one profile row; True when it existed.

## Functions

