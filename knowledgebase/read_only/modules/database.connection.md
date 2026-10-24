# Module `database.connection`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `database\connection.py`

```
Async database connection layer (aiosqlite) — single long-lived connection.

Why aiosqlite here (spec §4): the framework is asyncio-native; the old
synchronous sqlite3 calls ran INLINE on the event loop (logging, audit,
manager mutations) — ~0.2-0.4ms stalls per call, and under bursts they
compound. aiosqlite moves every SQLite operation onto ONE dedicated worker
thread behind an async queue: awaiting a query never blocks the loop.

Honest limits (documented, per spec): aiosqlite does NOT make SQLite
concurrently writable — the single connection serializes writers exactly
as before; it removes the EVENT-LOOP stall, not the single-writer rule.

Connection ownership (spec §6): exactly ONE connection per Database,
created by :meth:`DatabaseConnection.connect`, owned by the Database
service, closed by :meth:`close`. No per-query/per-log connections exist
anywhere in the codebase.

SQLite configuration (spec §8) — each pragma justified:
* ``journal_mode=WAL``        — readers never block the writer; the audit/
                                logs read paths (dashboard) stay responsive.
* ``busy_timeout=5000``       — the ONLY second connection (rare offline
                                inspection) waits instead of failing BUSY.
* ``foreign_keys=ON``         — new tables (credentials/cooldowns) declare
                                FKs; legacy tables keep their proven manual
                                cascades (reworking them buys nothing).
* ``synchronous=NORMAL``      — WAL default; durable-enough for derived
                                state (cooldowns/state) with far fewer
                                fsyncs than FULL. Secrets are tiny rows.

Transactions: ``async with conn.write()`` — explicit ``BEGIN IMMEDIATE``
on entry, COMMIT on success, ROLLBACK on exception. Beginning eagerly
(write lock up front) instead of SQLite's deferred default removes the
cross-connection upgrade race: a deferred writer that first reads holds
a read snapshot, and its later write returns SQLITE_BUSY immediately
(busy_timeout cannot retry a stale snapshot) the moment another
connection commits in between — exactly the "sim runner + live" pattern.
Repos never manage cursors/commits themselves.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `_TaskGuard` | class | no | Re-entrant (per asyncio task) mutual exclusion for the ONE connection. |
| `DatabaseConnection` | class | yes | One aiosqlite connection + transaction context, explicitly owned. |

### `_TaskGuard`

- `async def hold(self) -> AsyncIterator[bool]`
  - Yield True for the outermost holder, False when re-entered.
- `async def acquire_bounded(self, timeout: float) -> bool`
- `def release(self) -> None`

### `DatabaseConnection`

- `def connected(self) -> bool`
- `async def connect(self) -> None` — side effects: `network`
  - Open the connection and apply pragmas (idempotent, race-free).
- `async def close(self, timeout: float=CLOSE_TIMEOUT_S) -> None`
  - Close the connection (idempotent; safe during shutdown).
- `async def execute(self, sql: str, params: Sequence[Any]=()) -> None`
  - One statement, committed (simple writes / pragmas / DDL).
- `async def fetch_all(self, sql: str, params: Sequence[Any]=()) -> List[aiosqlite.Row]`
- `async def fetch_one(self, sql: str, params: Sequence[Any]=()) -> Optional[aiosqlite.Row]`
- `async def write(self) -> AsyncIterator[aiosqlite.Connection]`
  - Transaction: BEGIN IMMEDIATE, COMMIT on success, ROLLBACK on error.
- `async def read(self) -> AsyncIterator[aiosqlite.Connection]`
  - Read access that never observes another task's open transaction.
- `async def serialized(self) -> AsyncIterator[aiosqlite.Connection]`
  - Guard-only access: serialized, but NO transaction management.

## Functions

