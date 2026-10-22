# Module `database.repositories.account_plugins`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `database\repositories\account_plugins.py`

```
AccountPluginsRepository — direct account→plugin assignments.

One row in ``account_plugins`` means "this account runs this plugin"
(the NEW model: Account -> Plugins, no profile in between). The profile
tables (``profiles`` / ``profile_plugins`` / ``account_profiles``) stay
untouched for a future plugin-specific profile system and are neither
read nor written here.

This layer is persistence ONLY: it does NOT validate that the account or
the plugin exists (the manager above owns that), and it performs no
cascades — cleaning up an account's rows when the account is deleted is
the account-delete path's job, exactly like the other name-keyed
per-account tables (``account_profiles``, ``plugin_settings``, ...).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `AccountPluginsRepository` | class | yes | Direct plugin assignments, keyed by (account name, plugin name). |

### `AccountPluginsRepository`

- `async def assign_plugin(self, account: str, plugin: str) -> None`
  - Attach a plugin to an account (idempotent — re-assign is a no-op).
- `async def unassign_plugin(self, account: str, plugin: str) -> None`
  - Detach a plugin from an account (idempotent; never raises).
- `async def plugins_of(self, account: str) -> List[str]`
  - Sorted plugin names assigned to this account ([] when none —
- `async def set_plugins(self, account: str, plugins: Iterable[str]) -> List[str]`
  - FULL replacement: the account ends up with exactly ``plugins``.
- `async def all_assignments(self) -> Dict[str, List[str]]`
  - Every assignment as ``{account: [plugin, ...]}`` for boot restore.
- `async def set_profile(self, account: str, plugin: str, profile: Optional[str]) -> bool`
  - Bind the assignment to a profile (None = clear, back to defaults).
- `async def bindings(self, account: str) -> Dict[str, Optional[str]]`
  - ``{plugin: profile name or None}`` for every plugin assigned to
- `async def all_bindings(self) -> Dict[str, Dict[str, Optional[str]]]`
  - Every binding as ``{account: {plugin: profile or None}}``.
