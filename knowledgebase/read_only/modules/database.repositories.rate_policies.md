# Module `database.repositories.rate_policies`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `database\repositories\rate_policies.py`

```
RatePolicyRepository — the 4-level rate policy store (spec §5).

Rows live in ``rate_limit_policies`` keyed by (level, owner, action).
Owner is stored as TEXT ('' for global — the column is NOT NULL) and mapped
to/from RatePolicyRecord.owner (None for global) at the boundary.

Validation split (deliberate): this repository enforces *data integrity*
(finite numbers, 0 <= jitter_min <= jitter_max, valid level/owner shape);
the 1.5s safety floor is NOT checked here — that is
core.rate_limit_manager's job (UnsafePolicyError), because the floor is a
policy decision, not a storage invariant.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `RatePolicyRepository` | class | yes | get/set/delete/list for rate policies across the four levels. |

### `RatePolicyRepository`

- `async def set(self, level: str, owner: Optional[str], action: str, interval: float, jitter_min: float, jitter_max: float) -> None`
  - Upsert one policy row (level+owner+action is the key).
- `async def delete(self, level: str, owner: Optional[str], action: str) -> bool`
  - Remove one policy; True when a row was deleted.
- `async def delete_account(self, account: str) -> int`
  - Delete every account-scoped policy row for one account name.
- `async def get(self, level: str, owner: Optional[str], action: str) -> Optional[RatePolicyRecord]`
  - One policy by exact scope (None when nothing stored there).
- `async def list(self) -> List[RatePolicyRecord]`
  - Every stored policy (level, owner, action order).
