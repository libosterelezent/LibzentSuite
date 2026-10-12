# Module `database.repositories.accounts`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `database/repositories/accounts.py`

```
AccountsRepository — the persistent account registry (spec §1).

One row per account in ``accounts`` plus membership rows in
``account_profiles``. Dependent per-account tables (``account_settings``,
``plugin_settings``, ``plugin_states``, ``account_profiles``) are keyed by
account NAME — the public key everywhere — and removed explicitly by
:meth:`AccountsRepository.delete` (the schema declares no foreign keys, so
the cascade is manual, exactly like the audited old implementation).

Token VALUES never enter the database; only the ``.env`` key referencing
them (``token_ref``) is persisted.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `AccountsRepository` | class | yes | CRUD for accounts and their profile assignments, keyed by NAME. |

### `AccountsRepository`

- `async def add(self, name: str, *, token_ref: Optional[str]=None, enabled: bool=True, sync_group: Optional[str]=None) -> AccountRecord`
  - Insert a new account and return its persisted record.
- `async def get(self, name: str) -> Optional[AccountRecord]`
  - One account by name (None when unknown), with its profile list.
- `async def list(self) -> List[AccountRecord]`
  - All accounts ordered by name, each with its sorted profiles.
- `async def set_enabled(self, name: str, enabled: bool) -> None`
  - Flip the persisted enabled flag (False = skipped by start_all).
- `async def set_settings(self, name: str, settings: Dict[str, Any]) -> None`
  - Replace the account's whole framework-settings JSON blob.
- `async def delete(self, name: str) -> bool`
  - Delete an account AND every dependent row; True when it existed.
- `async def assign_profile(self, name: str, profile: str) -> None`
  - Attach a profile to an account (idempotent — re-assign is a no-op).
- `async def unassign_profile(self, name: str, profile: str) -> None`
  - Detach a profile from an account (idempotent; never raises).
- `async def profiles_of(self, name: str) -> List[str]`
  - Sorted names of the profiles assigned to this account ([] when none).
