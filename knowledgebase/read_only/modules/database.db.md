# Module `database.db`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `database\db.py`

```
Async SQLite persistence service (aiosqlite) — the app's single DB owner.

Lifecycle (spec §6): the constructor does NO I/O; ``await connect()``
opens the one long-lived aiosqlite connection, applies pragmas and runs
pending migrations; ``await close()`` shuts it down. Repositories are
stateless views over that connection (see database/connection.py for the
pragma rationale and the honest aiosqlite limits).

Migration strategy: ordered ``database.schema.MIGRATIONS`` applied inside
the write lock, each recorded in ``schema_migrations`` — repeatable
(already-applied versions are skipped), transactional per migration.

Secrets policy (unchanged, now stronger): the database stores Fernet
CIPHERTEXT for credentials (key outside the DB); it never stores
plaintext tokens.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `Database` | class | yes | Async persistence service owning ONE aiosqlite connection. |

### `Database`

- `def path(self) -> str`
- `async def connect(self) -> int` — side effects: `network`
  - Open the connection, apply pragmas, run pending migrations.
- `async def migrate(self) -> int` — side effects: `network`
  - Apply pending migrations in order (idempotent, recorded).
- `async def close(self) -> None`
  - Close the connection (idempotent; part of graceful shutdown).
