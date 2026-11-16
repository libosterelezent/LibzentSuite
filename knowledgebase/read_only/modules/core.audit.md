# Module `core.audit`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\audit.py`

```
Interaction audit trail: every Discord touch the framework makes.

Purpose (user spec): a detailed, in-depth view of how the selfbots interact
so behavior can be reviewed and fixed BEFORE it triggers Discord's
anti-abuse — plus a persistent record in the database.

Recorded per interaction: ``direction='out'`` rows carry every
rate-limited send (message/DM/reaction/button) with a content preview,
how long the limiter made us WAIT (``wait_ms``) and the human-like jitter
applied after the grant (``jitter_ms``); ``direction='in'`` rows carry
every message event dispatched to plugins.

Ported from the audited ``git show HEAD:core/audit.py`` with the rewrite's
renames: ``db_repo`` instead of ``database`` (duck-typed repository with
async ``add(entry)`` / ``recent(limit, account)``), ``jitter_ms`` instead
of the old ``delay_ms``, and a dict boundary — ``record()``/``tail()``/
``history()`` all speak JSON-safe dicts so the CLI, dashboard and
simulations share one shape. Kept from the original: the best-effort DB
write (now QUEUED — record() never touches SQLite, spec §29; one async
drain worker per recorder), per-subscriber error isolation, the ring
capacity bound, and the n<=0 tail guard. ``tail()`` stays synchronous
(ring-only); ``history()`` is async (awaits the repository, falls back to
the ring).

Consumers: the dashboard Audit page (live via WS + history via REST), the
CLI 'audit' command, scripts, and the simulation reports.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `AuditEntry` | dataclass | yes | One observed interaction (internal unit of the audit trail). |
| `AuditRecorder` | class | yes | Ring buffer + queued-async-DB sink + live subscribers for audit entries. |

### `AuditEntry`

- `def as_dict(self) -> Dict[str, Any]`
  - JSON-safe dict form (floats rounded to milliseconds precision).

### `AuditRecorder`

- `async def start_persistence(self, repo: Any, *, batch: int=50, interval: float=0.25) -> Optional['asyncio.Task']`
  - Spawn the async drain worker (call BEFORE traffic flows).
- `async def stop_persistence(self, timeout: float=5.0) -> None`
  - Stop the drain worker (idempotent); call BEFORE ``db.close()``.
- `def persistence_stats(self) -> Dict[str, int]`
  - Queued-persistence counters (db_failures / dropped).
- `def record(self, account: str, direction: str, action: str, channel_id: Optional[int]=None, content: str='', wait_ms: float=0.0, jitter_ms: float=0.0, note: str='') -> Dict[str, Any]`
  - Store one interaction (ring + queued best-effort DB) and notify
- `def tail(self, n: int=50, account: Optional[str]=None) -> List[Dict[str, Any]]`
  - Most recent entries as dicts (oldest first), optionally per account.
- `def subscribe(self, callback: Callable[[AuditEntry], None]) -> Callable[[], None]`
  - Live-tail subscription (dashboard WS); returns an unsubscribe callable.
- `async def history(self, limit: int=200, account: Optional[str]=None) -> List[Dict[str, Any]]`
  - DB-backed history (awaits the repository; falls back to the ring).

## Functions

- `def format_audit(entries: Iterable[Any]) -> List[str]`
  - Render entries (dicts or AuditEntry) as compact human-readable lines.
