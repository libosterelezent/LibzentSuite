# Module `core.profile_manager.manager`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `core/profile_manager/manager.py`

```
Profile manager (spec §4): named capability bundles assigned to accounts.

A profile names a set of plugins an account MAY run. Accounts can hold
several profiles; the effective grant is the UNION of their plugin lists.
An account with NO known profile is UNRESTRICTED (backwards compatible with
pre-profile configurations) — this mirrors the audited ``core/roles.py``
semantics exactly (``allowed_plugins`` returning ``None``).

Persistence is read-through into an in-memory cache: the repositories are
the source of truth, :meth:`load` (awaited once at boot, after
``database.connect()``) populates the cache and every mutating method
updates it right after the repository write succeeds. Mutating methods are
async (aiosqlite repos underneath); :meth:`allowed_plugins` stays SYNC and
answers from the cache because the account manager calls it from attach
paths that must not grow another await.

Ported from ``git show HEAD:core/roles.py`` (name regex, dedupe+sort plugin
lists, unknown-role warning behaviour) with the new repository API names.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `ProfileManager` | class | yes | CRUD + assignment for profiles; validates at every boundary. |

### `ProfileManager`

- `def forget_account(self, account: str) -> None`
  - Drop an account's assignment-cache entry (removal cascade).
- `async def load(self) -> None`
  - Populate the caches from the repositories (boot path).
- `async def set_profile(self, name: str, plugins: Any, description: str='') -> Dict[str, Any]` — side effects: `db_write`
  - Create or fully replace a profile (persisted via repo.upsert).
- `async def delete_profile(self, name: str) -> Dict[str, Any]`
  - Delete a profile (repo cascades account assignments).
- `async def profiles(self) -> List[Dict[str, Any]]`
  - Every profile as ``[{"name","description","plugins"}]``, sorted.
- `async def assign(self, account: str, profile: str) -> Dict[str, Any]`
  - Attach a profile to an account (persisted via accounts repo).
- `async def unassign(self, account: str, profile: str) -> Dict[str, Any]`
  - Detach a profile from an account (idempotent; persisted).
- `async def assigned(self, account: str) -> List[str]`
  - Sorted names of the profiles assigned to ``account`` ([] if none).
- `def allowed_plugins(self, account: str) -> Optional[Set[str]]`
  - UNION of plugin grants across the account's KNOWN profiles.
