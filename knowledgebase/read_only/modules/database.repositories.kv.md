# Module `database.repositories.kv`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `database/repositories/kv.py`

```
KvRepository — tiny generic key/value store (misc persistent state).

Used for one-off persistent facts (migration markers, scenario reports,
last-run bookkeeping...). Values are stored JSON-encoded so any
JSON-serializable type round-trips; a row that is NOT valid JSON (written
by hand or by an older version) is returned as its raw string instead of
crashing the read — the audited old code stored raw strings, so this
tolerance keeps old files readable.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `KvRepository` | class | yes | get/set/delete/prefix over the ``kv`` table. |

### `KvRepository`

- `async def get(self, key: str, default: Any=None) -> Any`
  - Value stored under key (JSON-decoded), or ``default`` when absent.
- `async def set(self, key: str, value: Any) -> None` — side effects: `db_write`
  - Store value under key (upsert; JSON-encoded so types round-trip).
- `async def delete(self, key: str) -> bool`
  - Remove one entry; True when a row was deleted.
- `async def prefix(self, p: str) -> Dict[str, Any]`
  - All entries whose key starts with ``p`` (key -> decoded value).
- `async def delete_prefix(self, p: str) -> int`
  - Delete every entry whose key starts with ``p``; returns the count.
