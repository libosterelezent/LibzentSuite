# Module `database.repositories.cache`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `database/repositories/cache.py`

```
Per-account persistent cache of Discord entities (spec add-on: "plugins
do discord api calls and then cache it in database for that profile only").

Rows are scoped to ONE account — account A's resolved DM channel is never
reused for account B (different Discord relationship). Kinds:

    "dm"      key=str(user_id)        -> channel_id (int)
    "user"    key=str(user_id)        -> {"id": int, "name": str}
    "channel" key=str(channel_id)     -> {"id": int, "name": str, "guild_id": int|None}

Why persist at all: re-opening a DM after every restart costs a fetch_user +
create_dm round trip per account per boot — exactly the redundant API
traffic that looks robotic. With the table, each entity costs ONE call for
the lifetime of the account. Rows are invalidated when Discord says a target
is gone (TargetGone) — a stale row then heals on the next resolution.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `CacheRepository` | class | yes | Read/write the account-scoped API entity cache. |

### `CacheRepository`

- `async def get(self, account: str, kind: str, key: str) -> Optional[Any]`
  - Cached value for (account, kind, key); None on a miss/corrupt row.
- `async def set(self, account: str, kind: str, key: str, value: Any) -> None`
  - Store one cached value (JSON-serialized).
- `async def delete(self, account: str, kind: str, key: str) -> bool`
  - Drop one cached value; True when a row existed.
- `async def drop_account(self, account: str, kind: Optional[str]=None) -> int`
  - Invalidate every cached entity for one account (optionally one
- `async def counts(self) -> Dict[str, int]`
  - Row counts per account (diagnostics for the Advanced page).
