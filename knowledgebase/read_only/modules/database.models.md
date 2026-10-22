# Module `database.models`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `database\models.py`

```
Database models: plain dataclasses crossing the persistence boundary.

These are the ONLY shapes the rest of the framework uses when talking about
persisted state. Repositories (database/repositories/*) construct/return them;
managers consume them. Keep this module dependency-free (no imports from
core.* beyond nothing) so database/ stays independently testable.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `AccountRecord` | dataclass | yes | PERSISTENT account data (spec §1: persistent vs runtime split). |
| `SyncGroupRecord` | dataclass | yes | A named sync group: accounts that act in sync (shared pacing). |
| `PluginProfileRecord` | dataclass | yes | A named per-plugin settings profile (migration v7). |
| `PluginMetadata` | dataclass | yes | A discovered plugin's manifest as persisted for the UI. |
| `RatePolicyRecord` | dataclass | yes | One stored rate policy row (level + owner scope it applies to). |

### `AccountRecord`

- `def to_dict(self) -> Dict[str, Any]`

### `SyncGroupRecord`

- `def to_dict(self) -> Dict[str, Any]`

### `PluginProfileRecord`

- `def to_dict(self) -> Dict[str, Any]`

### `PluginMetadata`

- `def to_dict(self) -> Dict[str, Any]`

### `RatePolicyRecord`

- `def to_dict(self) -> Dict[str, Any]`
