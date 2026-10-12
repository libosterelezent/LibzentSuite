# Module `core.cooldowns`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `core/cooldowns.py`

```
CooldownService — generic persistent cooldowns with restart recovery.

Core owns persistence, lookup, expiration and cleanup; plugins own WHAT a
cooldown means and WHEN to create/clear it (spec §22). Expiry uses
ABSOLUTE epoch timestamps, so the restart scenario works exactly as
specified:

    45-minute cooldown set -> framework down 10 minutes -> restart
    -> ~35 minutes REMAIN (from expires_at), never a fresh 45.

All methods are async (aiosqlite repository underneath) and account-
scoped by the first argument — Account A can never observe Account B's
cooldowns (spec §25). A ``time_fn`` seam keeps restart tests
deterministic without wall-clock sleeps.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `CooldownService` | class | yes | Persistent, account-scoped cooldowns with absolute expiry. |

### `CooldownService`

- `async def restore(self) -> Dict[str, int]` — side effects: `db_write`
  - Boot recovery: load live cooldowns, drop expired ones.
- `async def set(self, account: str, plugin: str, cooldown_key: str, duration_seconds: float, *, source: str='plugin', reason: str='', metadata: Optional[Dict[str, Any]]=None) -> Dict[str, Any]` — side effects: `db_write`
  - Start (or replace) a cooldown lasting ``duration_seconds`` from NOW.
- `async def get(self, account: str, plugin: str, cooldown_key: str) -> Optional[Dict[str, Any]]` — side effects: `db_write`
  - The ACTIVE cooldown row, or None when absent/expired.
- `async def remaining(self, account: str, plugin: str, cooldown_key: str) -> float`
  - Seconds left on a cooldown (0.0 when none/expired).
- `async def is_active(self, account: str, plugin: str, cooldown_key: str) -> bool`
- `async def clear(self, account: str, plugin: str, cooldown_key: str) -> bool` — side effects: `db_write`
  - Remove one cooldown (memory + DB).
- `async def active_for(self, account: str, plugin: Optional[str]=None) -> List[Dict[str, Any]]` — side effects: `db_write`
  - Every active cooldown of one account (optionally one plugin).
- `def active_count_for(self, account: str) -> int`
  - Memory-only active count (FINAL-PHASE F3: status-poll path).
- `async def resync(self) -> int` — side effects: `db_write`
  - Rebuild the memory mirror from the repository (DB wins per key).
- `async def delete_account(self, account: str) -> int` — side effects: `db_write`
  - Cascade on account deletion (spec §32).
