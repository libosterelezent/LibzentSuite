# Module `database.repositories.settings`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `database/repositories/settings.py`

```
AccountSettingsRepository — per-account framework settings (key -> value).

This is the key/value layer consumed by
``core.settings_manager.SettingsManager`` for framework account settings
(reconnect policy etc.). Rows live in ``account_settings`` keyed by account
NAME + setting key; values are JSON so types round-trip.

Extra beyond the pinned method set (deliberate, documented): ``list_accounts``
is probed by SettingsManager via ``hasattr`` to preload the framework layer
after a restart; without it, stored reconnect settings would be invisible
until first write. Removing it would silently drop functionality the spine
already codes against.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `AccountSettingsRepository` | class | yes | Framework account settings: get/set/delete keyed by (account, key). |

### `AccountSettingsRepository`

- `async def get(self, name: str) -> Dict[str, Any]`
  - All stored settings for one account ({} when none/corrupt).
- `async def set(self, name: str, key: str, value: Any) -> None` — side effects: `db_write`
  - Store one setting (upsert). Values must be JSON-serializable.
- `async def delete(self, name: str, key: str) -> bool`
  - Remove one setting; True when a row was actually deleted.
- `async def list_accounts(self) -> List[str]`
  - Sorted names of accounts that have at least one stored setting.
