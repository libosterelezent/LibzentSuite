# Module `database.repositories.sync_groups`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `database/repositories/sync_groups.py`

```
SyncGroupsRepository — first-class sync group rows (spec: sync groups).

Group identity + metadata live in ``sync_groups``; MEMBERSHIP stays on
``accounts.sync_group`` (the group name pointer — an account belongs to at
most one group, so a join table would add nothing the column does not
already say). Membership operations therefore go through the ACCOUNTS
table, in the same transaction style as the other account writes.

This layer is persistence ONLY: it knows nothing about the pacing engine
(``RateLimitManager`` runtime synchronization is SyncGroupManager's job).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `SyncGroupsRepository` | class | yes | CRUD for sync groups; membership via the accounts table. |

### `SyncGroupsRepository`

- `async def create(self, name: str, description: str='', *, enabled: bool=True) -> SyncGroupRecord`
  - Insert one group; returns its persisted record.
- `async def get(self, group_id: int) -> Optional[SyncGroupRecord]`
  - One group by id (None when unknown).
- `async def get_by_name(self, name: str) -> Optional[SyncGroupRecord]`
  - One group by name (None when unknown).
- `async def list(self) -> List[SyncGroupRecord]`
  - All groups ordered by name.
- `async def update(self, group_id: int, *, name: Optional[str]=None, description: Optional[str]=None, enabled: Optional[bool]=None) -> Optional[SyncGroupRecord]`
  - Update ONLY the given fields (None = keep); bumps updated_at.
- `async def delete(self, group_id: int) -> bool`
  - Delete one group row; True when it existed.
- `async def members(self, group_name: str) -> List[str]`
  - Account names whose membership points at this group (sorted).
- `async def membership_counts(self) -> Dict[str, int]`
  - group name -> member count (one GROUP BY; groups with no members
- `async def account_group_name(self, account: str) -> Optional[str]`
  - The group name this account points at (None when none/unknown).
- `async def set_account_group(self, account: str, group: Optional[str]) -> None`
  - Point an account's membership at a group (None = leave every
- `async def detach_members(self, group_name: str) -> List[str]`
  - Clear the membership of every member; returns their names.

## Functions

