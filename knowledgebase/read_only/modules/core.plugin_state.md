# Module `core.plugin_state`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/plugin_state.py`

```
PluginStateService — generic scoped persistence for plugins (spec §20).

A tiny approved key/value API over the ``kv`` table so plugins never
touch SQL or connections (spec §31). Scopes:

    global           — every account, every plugin instance
    account          — one account (any plugin using this service)
    plugin           — one plugin across accounts
    account_plugin   — one (account, plugin) pair

Storage keys are namespaced (``pstate:<scope>:...``) inside kv. Values are
stored RAW: the kv repository JSON-encodes on write and decodes on read
(one layer of encoding — values used to be pre-encoded here, double-encoding
every row; legacy rows are still decoded transparently in ``_get``).
All methods async (aiosqlite underneath). Plugins obtain
this service through PluginServices (approved plugin API), not directly.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `PluginStateService` | class | yes | Scoped get/set/delete/exists for plugin-persistent state. |

### `PluginStateService`

- `async def get_global(self, key: str, default: Any=None) -> Any`
- `async def set_global(self, key: str, value: Any) -> None`
- `async def get_account(self, account: str, key: str, default: Any=None) -> Any`
- `async def set_account(self, account: str, key: str, value: Any) -> None`
- `async def delete_account(self, account: str, key: str) -> bool`
- `async def get_plugin(self, plugin: str, key: str, default: Any=None) -> Any`
- `async def set_plugin(self, plugin: str, key: str, value: Any) -> None`
- `async def get_pair(self, account: str, plugin: str, key: str, default: Any=None) -> Any`
- `async def set_pair(self, account: str, plugin: str, key: str, value: Any) -> None`
- `async def delete_pair(self, account: str, plugin: str, key: str) -> bool`
- `async def cascade_account(self, account: str) -> int`
  - Delete EVERY kv row scoped to one account (both account scopes).
