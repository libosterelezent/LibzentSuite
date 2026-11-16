# Module `core.event_bus`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\event_bus.py`

```
Event bus + the framework's typed event vocabulary (spec §11).

Guarantees (ported from the audited original):
  * one failing handler never breaks other handlers (isolation),
  * subscribers registered for a base class receive subclass events,
  * publish() can be awaited, publish_nowait() is safe from sync callbacks,
  * fire-and-forget tasks hold strong references (no GC mid-dispatch).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `Event` | dataclass | yes | Base event. ts defaults to wall clock; account is "" when not per-account. |
| `AccountConnected` | dataclass | yes | An account's client connected and its cache was ingested. |
| `AccountDisconnected` | dataclass | yes | An account's client dropped (reason carries the why). |
| `AccountFailed` | dataclass | yes | An account failed to start or reconnect. |
| `PluginLoaded` | dataclass | yes | A plugin instance was created + loaded on an account. |
| `PluginUnloaded` | dataclass | yes | A plugin instance was unloaded from an account. |
| `PluginEnabledChanged` | dataclass | yes | A plugin was enabled or disabled on an account. |
| `TaskQueued` | dataclass | yes | A task entered the scheduler queue. |
| `TaskStarted` | dataclass | yes | A worker picked the task up (rate grant acquired, sender starting). |
| `TaskCompleted` | dataclass | yes | A task finished successfully. |
| `TaskFailed` | dataclass | yes | A task failed permanently (retries exhausted or non-retryable). |
| `TaskCancelled` | dataclass | yes | A queued/running task was cancelled. |
| `RateLimitTriggered` | dataclass | yes | Pacing or a server-provided limit delayed an action. |
| `SettingChanged` | dataclass | yes | One setting changed somewhere (plugin default layer, account layer...). |
| `MessageReceived` | dataclass | yes | A Discord message seen by an account (never the account's own sends). |
| `MessageEdited` | dataclass | yes | A message was edited (stable ids + the RICH post-edit message). |
| `MessageDeleted` | dataclass | yes | A message was deleted (ids only — the library gives no payload body). |
| `ReactionAdded` | dataclass | yes | A reaction was added to a message — WITHOUT any message fetch (§9). |
| `ScenarioStarted` | dataclass | yes | A simulation scenario began. |
| `ScenarioFinished` | dataclass | yes | A simulation scenario ended (passed flag included). |
| `_HandlerList` | class | no | Handler list with a mutation version + cached dispatch snapshot. |
| `EventBus` | class | yes | Minimal async pub/sub with subclass-aware dispatch and error isolation. |

### `Event`

- `def name(self) -> str`

### `_HandlerList`

- `def append(self, handler) -> None`
- `def remove(self, handler) -> None`
- `def snapshot(self) -> List[Handler]`

### `EventBus`

- `def subscribe(self, event_type: Type[Event], handler: Handler) -> Callable[[], None]`
  - Register handler for event_type (and its subclasses).
- `async def drain(self, timeout: float=2.0) -> None` — side effects: `creates_tasks, sleeps`
  - Await the fire-and-forget dispatch backlog (shutdown tail).
- `def publish_nowait(self, event: Event) -> Optional['asyncio.Task[None]']` — side effects: `creates_tasks, publishes_event`
  - Fire-and-forget publish; safe from sync callbacks inside a loop.
- `def stats(self) -> Dict[str, int]`
  - Queue depth + counters for BOTH queues (dashboard/observability).
- `async def publish(self, event: Event) -> List[Any]` — side effects: `publishes_event`
  - Invoke all matching handlers (sync or async), isolated per handler.
