# Module `core.settings_manager`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\settings_manager.py`

```
Shared settings system (spec §3): ONE schema, consumed by CLI + GUI alike.

Design:
  * Plugins (and the framework's account layer) declare settings as a tuple of
    `Setting` objects — type, default, bounds, human description.
  * `SettingsManager` resolves the effective values by merging layers:
        1. schema defaults          (lowest priority)
        2. global layer             (config/global_settings.json)
        3. account layer            (database plugin_settings / account_settings)
  * The SAME schema auto-generates the dashboard settings editors and the CLI
    'set' validation — no per-surface implementations (parity by construction).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `Setting` **PLUGIN API** | dataclass | yes | One typed setting: name, type, default, bounds, human description. |
| `SettingsManager` | class | yes | Layered, validated, persisted settings for plugins AND accounts. |

### `Setting` — PLUGIN API

- `def validate(self, value: Any) -> Any`
  - Coerce + bounds-check a raw value; returns the typed value.
- `def as_manifest(self) -> Dict[str, Any]`
  - JSON-safe manifest entry for auto-generated settings UIs.

### `SettingsManager`

- `async def load(self) -> None` — side effects: `network`
  - Pull the persisted account layers into memory (boot path).
- `def forget_account(self, account: str) -> None`
  - Drop an account's in-memory mirror layers (removal cascade).
- `def resolve(self, plugin_name: str, schema: Sequence[Setting], account: Optional[str]=None, *, profile: Optional[Mapping[str, Any]]=None) -> Dict[str, Any]`
  - Effective, validated settings for one plugin instance.
- `async def set_plugin_setting(self, account: str, plugin: str, key: str, value: Any, schema: Sequence[Setting]) -> Any` — side effects: `publishes_event, db_write`
  - Validate + persist one account-layer plugin setting; returns typed value.
- `async def clear_plugin_setting(self, account: str, plugin: str, key: str, schema: Sequence[Setting]=(), *, profile: Optional[Mapping[str, Any]]=None) -> Any` — side effects: `publishes_event`
  - DELETE one account-layer plugin setting override (dashboard 'reset').
- `def account_settings(self, account: str) -> Dict[str, Any]`
  - Resolved framework account settings (reconnect policy...).
- `async def set_account_setting(self, account: str, key: str, value: Any) -> Any` — side effects: `publishes_event, db_write`
  - Validate + persist one framework account setting.
- `def layers_view(self) -> Dict[str, Any]`
  - Both layers as JSON-safe dicts (dashboard Advanced page).

## Functions

