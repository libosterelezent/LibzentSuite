# EVENT_BUS — typed pub/sub with subclass dispatch and handler isolation

> Source of truth: `core/event_bus.py`.
> Per-module reference: `read_only/modules/core.event_bus.md`.

`EventBus` is the framework's ONLY internal broadcast mechanism: managers
publish typed dataclass events; subscribers register for an event type (or the
base `Event`) and receive every match. Guarantees ported from the audited
original:

- one failing handler never breaks other handlers (**isolation by design**),
- subscribers registered for a base class receive **subclass** events,
- `publish()` can be awaited; `publish_nowait()` is safe from sync callbacks
  inside a loop,
- fire-and-forget tasks hold **strong references** (no GC mid-dispatch).

```
   emitters (publish_nowait unless noted)                    subscribers
 ┌──────────────────────────────┐   subscribe(EventType, handler)
 │ AccountManager  ─ Account*, Message*, Reaction*          │
 │ TaskScheduler   ─ Task*                                  │─► PluginManager._on_message
 │ RateLimitManager ─ RateLimitTriggered (awaited publish)  │─► RemoteControl._on_message
 │ SettingsManager ─ SettingChanged                          │─► TaskAckRegistry (Task* ×3)
 │ PluginManager   ─ Plugin*                                 │─► AccountManager._audit_inbound
 │ SimulationRunner ─ Scenario* (awaited publish)            │─► AccountPluginServices wait/collect
 └──────────────────────────────┘                            │─► bootstrap._on_event → LogService
                                                             │─► DashboardServer (Task* → WS)
                                                             └► unsubscribe() (idempotent)
```

---

## 1. Bus mechanics (`EventBus`)

| Member | Behavior |
|---|---|
| `subscribe(event_type, handler) -> Callable[[], None]` | appends the handler to `self._subs[event_type]`; returns an idempotent unsubscribe callable (a `ValueError` from `.remove` is swallowed) |
| `publish_nowait(event) -> asyncio.Task[List[Any]]` | fire-and-forget: `get_running_loop().create_task(self.publish(event))`; the task is added to `self._background` (strong ref) and discarded via done-callback — the documented `create_task` GC hazard guard. Safe from sync callbacks inside a running loop. |
| `publish(event) -> List[Any]` (async) | invokes every matching handler (sync or async — coroutine outcomes are awaited), returns handler results / exception objects **in invocation order** |
| `subscriber_count(event_type=None)` | number of subscriptions, optionally per type |

### Subclass-aware dispatch
`publish` iterates `self._subs.items()` (a `list()` snapshot — subscriptions
may change during dispatch) and dispatches to `(event_type, handlers)` pairs
where `isinstance(event, event_type)`. Consequences:

- subscribing to `Event` receives **everything** (bootstrap's structured event
  logging does exactly this);
- subscribing to `MessageReceived` does NOT receive `MessageEdited` — they are
  siblings, not subclasses;
- every event is a `@dataclass` subclass of `Event`, so all carry `account`
(default `""`) and `ts` (wall clock, default `time.time()`); `event.name` is
`type(self).__name__`.

### Handler isolation rules
Per handler, `publish` catches:

- **`asyncio.CancelledError`** — counts as a handler FAILURE unless the
  publish task itself is being cancelled (outer cancellation test:
  `task.cancelling() > task.uncancel()` → re-raise). An internally cancelled
  handler is logged (`LOG.exception`) and its `CancelledError("handler
  cancelled")` object appended to results — one handler's cancellation never
  kills the dispatch loop.
- **`KeyboardInterrupt` / `SystemExit`** — ALWAYS propagate (process-level
  signals must not be swallowed by isolation).
- **any other `Exception`** — logged with traceback and appended to results;
  the remaining handlers still run.

Handlers list is snapshotted (`list(handlers)`) so a handler may subscribe or
unsubscribe others mid-dispatch safely.

### publish vs publish_nowait — which to use
- `await bus.publish(event)`: the caller waits for ALL handlers; used when
  ordering is contractual — the rate engine's `RateLimitTriggered`
  (`_publish` in `core/rate_limit_manager/engine.py`: "sync subscribers see
  the event before acquire() returns — deterministic for tests") and the
  simulator's `ScenarioStarted`/`ScenarioFinished`.
- `bus.publish_nowait(event)`: fire-and-forget from managers
  (`AccountManager`, `TaskScheduler._emit`, `PluginManager`,
  `SettingsManager`, `AccountPluginServices.publish`); the scheduler wraps it
  so a bus failure ("no running loop; dropped event ..." / "event publish
  failed") is logged and never breaks the send pipeline.

### Ordering guarantees
- Within one `publish`: handlers run in subscription order per event type,
  event types in first-subscription order; results mirror invocation order.
  Sync handlers complete before the next; async handlers are awaited inline.
- Across `publish_nowait` calls: tasks are created in call order and the loop
  runs them FIFO within a tick — events published later start dispatch later,
  but an async handler that awaits can interleave with the next event's
  dispatch. Only `await publish` gives full completion ordering.

---

## 2. Full event catalog

Fields listed beyond the inherited `Event(account="", ts=time.time())`.
"Emitters/consumers" verified against publish/subscribe call sites (production
code; tests exercise nearly every type).

### Accounts

| Event | Fields | Emitted by | Consumed by |
|---|---|---|---|
| `AccountConnected` | — | `core.account_manager.manager.AccountManager` (account start + reconnect success) | bootstrap event logging (structured INFO line) |
| `AccountDisconnected` | `reason: str = ""` | `AccountManager.stop` path (`reason="stopped"`) | bootstrap event logging |
| `AccountFailed` | `error: str = ""` | `AccountManager` reconnect loop (`rt.last_error`) | bootstrap event logging |

### Plugins

| Event | Fields | Emitted by | Consumed by |
|---|---|---|---|
| `PluginLoaded` | `plugin: str = ""` | `core.plugin_manager.manager.PluginManager` after instance creation + load | bootstrap event logging |
| `PluginUnloaded` | `plugin: str = ""` | `PluginManager` unload | bootstrap event logging |
| `PluginEnabledChanged` | `plugin: str = ""`, `enabled: bool = True` | `PluginManager.set_enabled` | bootstrap event logging |

### Tasks (all `publish_nowait` via `TaskScheduler._emit`)

| Event | Fields | Emitted by | Consumed by |
|---|---|---|---|
| `TaskQueued` | `task_id: int`, `plugin: str`, `action: str` | `TaskScheduler.submit` | — (no production subscriber; intentionally NOT in bootstrap's `_LOGGED_EVENTS` — too chatty. The dashboard's Tasks page reads via REST, not the bus) |
| `TaskStarted` | `task_id: int`, `action: str` | `TaskScheduler._run_task` (queued→running) | dashboard WS push (`TASK_EVENT_TYPES`) |
| `TaskCompleted` | `task_id: int`, `duration_s: float`, `summary: str` | `TaskScheduler._finish` | `TaskAckRegistry._on_completed` (resolves the plugin's await), dashboard WS, bootstrap event logging |
| `TaskFailed` | `task_id: int`, `error: str`, `error_kind: str` (exception CLASS NAME, e.g. `"TargetGone"` — see [SCHEDULER.md](SCHEDULER.md) `_error_kind`) | `TaskScheduler._finish` (FAILED and TIMED_OUT) | `TaskAckRegistry._on_failed` (reconstructs `FlowBlocked` family across the ack seam), dashboard WS, bootstrap event logging |
| `TaskCancelled` | `task_id: int` | `TaskScheduler._finish` | `TaskAckRegistry._on_cancelled` (raises `SchedulerError("task ... was cancelled")` to the waiter), dashboard WS, bootstrap event logging |

### Rate limits

| Event | Fields | Emitted by | Consumed by |
|---|---|---|---|
| `RateLimitTriggered` | `action: str`, `wait_s: float`, `reason: str` (field comment says `"pacing" \| "server"`, but `acquire()` forwards the Grant reason, which can also be `"lane"` when the send lane contributed — see [RATE_LIMITING.md](RATE_LIMITING.md); reported as a doc mismatch) | `RateLimitManager.acquire` via awaited `_publish` (only when `wait_s > 0`) | bootstrap event logging; tests |

### Settings

| Event | Fields | Emitted by | Consumed by |
|---|---|---|---|
| `SettingChanged` | `scope: str` (e.g. `"account:alpha/plugin:soulcopy/key:trigger"`), `key: str`, `old_value: Any`, `new_value: Any` | `core.settings_manager.SettingsManager` (set/delete paths, `publish_nowait`) | no production subscriber at time of writing — emitted for observability/extension (documented gap, not dead code: the settings layer pushes it on every change) |

### Messages (the rich inbound surface)

| Event | Fields | Emitted by | Consumed by |
|---|---|---|---|
| `MessageReceived` | flat: `message_id: int`, `channel_id: int`, `author_id: int`, `content: str`, `dm: bool`; rich: `message: Any` (the `adapters.model.DiscordMessage` DTO, `None` for legacy/synthetic events) | `AccountManager` adapter ingestion (`on_message`); `AccountPluginServices.publish` (synthetic/plugin-side) | `PluginManager._on_message` (dispatch to per-account plugin instances), `RemoteControl._on_message` (mention prefix), `AccountManager._audit_inbound` (direction `'in'` audit row), `AccountPluginServices.wait_for_message` / `collect_messages` (scoped subscribe/unsubscribe) |
| `MessageEdited` | `message_id`, `channel_id`, `guild_id: Optional[int]`, `message: Any` (rich post-edit `DiscordMessage`, `raw` = library message) | `AccountManager` adapter ingestion | `PluginManager._on_message` |
| `MessageDeleted` | `message_id`, `channel_id`, `guild_id: Optional[int]` (ids only — the library gives no payload body) | `AccountManager` adapter ingestion | `PluginManager._on_message` |
| `ReactionAdded` | `message_id`, `channel_id`, `guild_id`, `user_id: int`, `emoji: str` (printable), `dm: bool`, `raw: Any` (runtime-only library objects, `repr=False`) | `AccountManager` adapter ingestion | `PluginManager._on_message` |

### Simulation

| Event | Fields | Emitted by | Consumed by |
|---|---|---|---|
| `ScenarioStarted` | `name: str` | `adapters.simulator.runner.SimulationRunner.run` (awaited `publish`) | bootstrap event logging |
| `ScenarioFinished` | `name: str`, `passed: bool`, `duration_s: float` | `SimulationRunner.run` (awaited `publish`) | bootstrap event logging |

---

## 3. The `MessageReceived` contract — flat compat + rich DTO

`MessageReceived` is the compatibility seam between the original flat event
surface and the rich adapter model (commit `35176a8`'s
"rich Discord message/event boundary"):

```python
@dataclass
class MessageReceived(Event):
    message_id: int = 0
    channel_id: int = 0
    author_id: int = 0
    content: str = ""
    dm: bool = False
    message: Any = None   # rich DTO — additive, optional
```

- **The five flat fields are the original surface every existing consumer
  uses** (soulcopy, roll, remote control, collect/wait) — they remain the
  stable, always-present contract.
- **`.message` carries the RICH `adapters.model.DiscordMessage`**: embeds,
  components, attachments, reactions, reference, author/channel/guild,
  `created_at`, and the runtime-only `raw` escape hatch (the live library
  object). It is typed `Any` so `core` never imports the adapter layer —
  the dependency points the other way.
- `None` for legacy/synthetic events that predate the rich boundary:
  consumers must treat `.message` as optional and fall back to the flat
  fields.
- Direction: `MessageReceived` is NEVER the account's own sends (inbound
  only); every one is mirrored into the audit trail with direction `'in'` by
  `AccountManager._audit_inbound`.
- The same dual shape (stable ids + optional rich `.message`) carries to
  `MessageEdited`; `MessageDeleted` stays ids-only because the library
  provides no payload body; `ReactionAdded` is fully self-contained by design
  (§9 of the adapter spec: no message fetch needed — everything a
  kakerareact-style plugin needs arrives on the event).

---

## 4. Bus-created-task hazards (why strong refs)

Both `publish_nowait` and every fire-and-forget manager task keep explicit
strong references (`EventBus._background`, `TaskScheduler._worker_refs`,
`_GrantGate._cycles`, `AccountManager._background`) with done-callbacks that
discard them. This is the documented asyncio hazard: an unreferenced task can
be garbage-collected mid-dispatch, silently dropping the remainder of a
dispatch (and its handlers' side effects). If you add a fire-and-forget path
over the bus, follow the same pattern.

---

## Coalesced dispatch (final-phase F5-c + G6-A′)

> Source of truth: `core/event_bus.py`. Verified at commit `487dc53`.

`publish_nowait` no longer creates one Task per event (measured ~49 µs of
pure task machinery per event — 65% of the lean dispatch path and the
source of the ~22 ms burst-callback lag ceiling). Events append to a
**bounded FIFO** (`MAX_QUEUE = 20_000`, drop-newest + counted + burst-
limited logging) drained by **one dispatcher task** that yields every
`SLICE_EVENTS = 128` events / `SLICE_BUDGET_S = 0.004` — a burst occupies
the loop in bounded slices instead of one long callback. End-to-end:
16.4k → 34.4k msg/s; loop-lag p50 30.6 → 7.6 ms.

`publish()` dispatches through a **subscriber-set cache** (per concrete
event type, one tuple built per subscription epoch, invalidated wholesale
on any subscribe/unsubscribe) + per-list versioned snapshots. Ordering is
INSERTION order (dict order), not MRO — the MRO variant was rejected
because production subscribes the base `Event` logger FIRST. Awaits
publish at 4.6 µs/event, shape-independent.

**Per-account waiter registry** (`MessageWaiterRegistry`,
`core/account_manager/services.py`): `wait_for_message` /
`collect_messages` route through one shared per-account index instead of
unfiltered bus subscriptions — waiter fan-out M×E → M/A; a waiter for
account A structurally never runs for B's events.

The deferral invariant is preserved and tested: dispatch happens across
an `await`, never in the publisher's own step (the TaskAck no-lost-ack
guarantee). `drain()` re-checks the queue until stable; the dispatcher's
empty-check and exit happen in the same step (no lost-wakeup race).
`stats()` exposes `queued/dispatched/dropped`.
