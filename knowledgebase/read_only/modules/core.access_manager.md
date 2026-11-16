# Module `core.access_manager`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\access_manager.py`

```
AccessManager — the DB-backed authorization/prefix/admin store.

This is the admin panel's data layer, replacing the env-based
OWNER_USER_IDS model (see the migration note in credentials.py).

Storage: single kv row per concern under the ``access:`` prefix —

  access:admins          -> [discord user id, ...]
  access:mudae_bot_id    -> int
  access:account:<name>  -> {"is_admin": bool,
                             "prefix_mode": "mention"|"symbol"|"both",
                             "prefix_symbol": str,
                             "allowed_user_ids": [int, ...]}

Fail-closed semantics:
  * empty per-account allowed_user_ids -> NOBODY may invoke that
    account's commands (except admin ids / admin-flagged accounts)
  * an unknown/absent account row -> same as empty
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `AccessManager` | class | yes | CRUD + gate lookups over the access kv rows. |

### `AccessManager`

- `async def admin_ids(self) -> List[int]`
- `async def set_admin_ids(self, ids: List[int]) -> List[int]`
- `async def add_admin(self, user_id: int) -> List[int]`
- `async def remove_admin(self, user_id: int) -> List[int]`
- `async def migrate_from_credentials(self) -> int`
  - Seed the admin list from OWNER_USER_IDS when the DB list is
- `async def account_view(self, name: str) -> Dict[str, Any]`
  - The account's access row (copies; safe to mutate).
- `async def set_admin_flag(self, name: str, is_admin: bool) -> Dict[str, Any]`
- `async def allowed_user_ids(self, name: str) -> List[int]`
- `async def set_allowed_user_ids(self, name: str, ids: List[int]) -> List[int]`
- `async def add_allowed(self, name: str, user_id: int) -> List[int]`
- `async def remove_allowed(self, name: str, user_id: int) -> List[int]`
- `async def prefix_config(self, name: str) -> Dict[str, Any]`
- `def prefix_config_sync(self, name: str) -> Optional[Dict[str, Any]]`
  - Last-known per-account prefix config (no I/O — the factory
- `async def warm_prefix_cache(self, names=None) -> None`
  - Load prefix rows for the given accounts (all when None).
- `async def set_prefix_config(self, name: str, mode: str, symbol: str='') -> Dict[str, Any]`
- `async def mudae_bot_id(self) -> Optional[int]`
  - DB value first; the env MUDAE_BOT_ID is the fallback.
- `async def set_mudae_bot_id(self, user_id: Optional[int]) -> None`
- `async def all_account_rows(self) -> Dict[str, Dict[str, Any]]`
  - Every access row keyed by account name (adminlist verb + the
- `async def forget_account(self, name: str) -> None`
  - Removal cascade: delete the account's access row (admin flag,
- `async def is_admin_id(self, user_id: Optional[int]) -> bool`
  - Admin = id in the admin list OR the user id of an
- `async def set_account_user_id(self, name: str, user_id: Optional[int]) -> None`
  - Record the account's Discord user id in its access row (the
- `async def can_invoke(self, account: str, user_id: Optional[int]) -> bool`
  - THE command gate: per-account allowed list, admin bypass,

## Functions

- `def account_key(name: str) -> str`
