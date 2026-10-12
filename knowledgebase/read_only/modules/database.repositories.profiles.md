# Module `database.repositories.profiles`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `database/repositories/profiles.py`

```
ProfilesRepository — named capability bundles (spec §4).

A profile is a set of plugin names an account may run, plus a description.
Rows live in ``profiles`` (name/description) and ``profile_plugins``
(membership). Ported from the audited old roles code: plugin lists are
deduplicated and sorted on save, and deleting a profile also drops its
memberships and any account assignments.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `ProfilesRepository` | class | yes | CRUD for profiles, keyed by profile name. |

### `ProfilesRepository`

- `async def upsert(self, name: str, plugins: Iterable[str], description: str='') -> None` — side effects: `db_write`
  - Create or fully replace a profile (one transaction).
- `async def delete(self, name: str) -> bool`
  - Delete a profile, its plugin memberships AND its account
- `async def get(self, name: str) -> Optional[ProfileRecord]`
  - One profile with its sorted plugin list (None when unknown).
- `async def list(self) -> List[ProfileRecord]`
  - All profiles ordered by name, each with its sorted plugins.
