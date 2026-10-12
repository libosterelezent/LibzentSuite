# Module `core.sync_group_manager.manager`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `core/sync_group_manager/manager.py`

```
SyncGroupManager — the application-level owner of sync group membership.

Division of labour (the managers cooperate, none absorbs another):

  * SyncGroupManager (this)  — persistent group rows + which account is in
                               which group; the ONLY writer of membership.
  * RateLimitManager         — runtime PACING. Grouped accounts serialize on
                               the shared ``group:<name>`` clock exactly as
                               before; this manager only keeps the engine's
                               account->group metadata in sync through the
                               existing ``set_account_group`` API.
  * ProfileManager           — plugin permissions (orthogonal: changing a
                               profile never touches a group, and vice versa).
  * AccountManager           — account lifecycle; delegates the group bits
                               of add/remove/restore here when wired.

Persistence model (see database/repositories/sync_groups.py): group rows in
``sync_groups`` (id = stable identity, name = unique pointer), membership
on ``accounts.sync_group``. An account is in AT MOST one group.

Boot relationship: AccountManager.load() -> sync_runtime() here -> the
limiter mapping. Every membership mutation persists FIRST (repository
transaction), then synchronizes the limiter mapping — persist+sync run
under one manager lock so concurrent moves of the same account (A->Alpha
racing A->Beta) always leave the database and the runtime mapping agreeing.

Legacy compatibility: names that predate first-class groups (or arrive via
the legacy ``accounts sync_group`` field on creation) are ADOPTED verbatim
— ``sync_membership`` get-or-creates the group row, exactly like migration
v5 backfills one group per distinct legacy name.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `SyncGroupManager` | class | yes | CRUD + membership for sync groups; keeps the limiter metadata true. |

### `SyncGroupManager`

- `async def load(self) -> None` — side effects: `db_write`
  - Populate the group cache from the repository (boot path).
- `async def sync_runtime(self) -> int`
  - Boot restore: push every PERSISTED membership into the limiter.
- `async def create_group(self, name: str, description: str='', *, enabled: bool=True) -> Dict[str, Any]` — side effects: `db_write`
  - Create a group (may exist with zero members).
- `async def get_group(self, group: GroupRef) -> Optional[Dict[str, Any]]`
  - One group by id or name (None when unknown).
- `async def get_group_by_name(self, name: str) -> Optional[Dict[str, Any]]`
  - One group by name (None when unknown).
- `async def list_groups(self) -> List[Dict[str, Any]]` — side effects: `db_write`
  - Every group as ``{"id","name","description","enabled",
- `async def update_group(self, group: GroupRef, *, name: Optional[str]=None, description: Optional[str]=None, enabled: Optional[bool]=None) -> Dict[str, Any]` — side effects: `db_write`
  - Update the given fields; a rename also rewrites every member's
- `async def delete_group(self, group: GroupRef, *, detach_members: bool=False) -> Dict[str, Any]` — side effects: `db_write`
  - Delete a group. Its ACCOUNTS are never deleted.
- `async def add_account_to_group(self, group: GroupRef, account: str) -> Dict[str, Any]` — side effects: `db_write`
  - Put an account in a group (replaces any previous membership:
- `async def remove_account_from_group(self, group: GroupRef, account: str) -> Dict[str, Any]` — side effects: `db_write`
  - Remove an account from ONE group (group -> None).
- `async def set_account_group(self, account: str, group: Optional[GroupRef]) -> Dict[str, Any]` — side effects: `db_write`
  - The membership transition primitive: None -> G, G1 -> G2,
- `async def clear_account_group(self, account: str) -> Dict[str, Any]`
  - Remove the account from any group (back to per-account pacing).
- `async def get_group_members(self, group: GroupRef) -> List[str]`
  - Sorted account names of the group's members ([] when unknown).
- `async def get_account_group(self, account: str) -> Optional[str]` — side effects: `db_write`
  - The account's group NAME (None when in no group / unknown).
- `async def sync_membership(self, account: str, group: Optional[str]) -> None` — side effects: `db_write`
  - Adopt a membership written by a LEGACY path and sync the runtime.
- `async def forget_account(self, account: str) -> None`
  - Removal cascade: clear the account's runtime group mapping.
- `async def snapshot(self, group: GroupRef) -> Dict[str, Any]`
  - Application-level group snapshot (JSON-safe).
