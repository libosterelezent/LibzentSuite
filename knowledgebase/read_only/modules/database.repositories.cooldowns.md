# Module `database.repositories.cooldowns`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `database/repositories/cooldowns.py`

```
Persistent cooldown repository (spec §21/§22).

Cooldowns persist ABSOLUTE epoch expiry (``expires_at``) so a restart
restores the REMAINING time instead of restarting the window. The core
CooldownService owns persistence/lookup/expiry/cleanup; plugins decide
what a cooldown MEANS — nothing here is plugin-specific.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `CooldownRepository` | class | yes | CRUD + expiry-aware queries over the ``cooldowns`` table. |

### `CooldownRepository`

- `async def upsert(self, *, account: str, plugin: str, cooldown_key: str, started_at: float, expires_at: float, source: str='', reason: str='', metadata: Optional[Dict[str, Any]]=None) -> None` — side effects: `db_write`
  - Insert or replace one cooldown (same identity = same row).
- `async def active_for(self, account: str, plugin: Optional[str]=None, now: Optional[float]=None) -> List[Dict[str, Any]]`
  - Non-expired cooldowns for an account (optionally one plugin).
- `async def active_all(self, now: Optional[float]=None) -> List[Dict[str, Any]]`
  - Every non-expired cooldown of EVERY account in ONE query
- `async def get(self, account: str, plugin: str, cooldown_key: str, now: Optional[float]=None) -> Optional[Dict[str, Any]]`
  - One cooldown (None when absent or already expired).
- `async def clear(self, account: str, plugin: str, cooldown_key: str) -> bool`
  - Remove one cooldown; True when it existed.
- `async def clear_expired_for(self, account: str, plugin: str, cooldown_key: str, now: float) -> bool` — side effects: `db_write`
  - Delete one cooldown ONLY when still expired at ``now``.
- `async def clear_expired(self, now: Optional[float]=None) -> int`
  - Delete every expired row; returns how many (boot cleanup).
- `async def delete_account(self, account: str) -> int`
  - Drop every cooldown owned by an account (deletion cascade).
