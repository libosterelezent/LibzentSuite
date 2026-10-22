# Module `core.account_manager.services`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\account_manager\services.py`

```
PluginServices implementation for the account manager (A6).

Implements the :class:`core.plugin_base.PluginServices` protocol by duck
typing (no import of ``core.plugin_manager`` — that package is built in
parallel and is never needed here at runtime).

DESIGN — how sends await task completion (the lead asked for this):
    ``send_message``/``send_dm`` submit a task into the scheduler and then
    AWAIT its completion through a shared :class:`TaskAckRegistry`:

      * the AccountManager builds ONE registry per process and hands it to
        every services instance (the registry subscribes ONCE to
        ``TaskCompleted`` / ``TaskFailed`` / ``TaskCancelled`` on the bus),
      * ``send_*`` registers a future under ``task.id`` and waits with a
        bounded timeout (``task_ack_timeout``, default 30s),
      * ``TaskCompleted`` resolves the future with the event's summary,
        ``TaskFailed``/``TaskCancelled`` raise :class:`core.errors.SchedulerError`
        carrying the reason, a timeout raises ``SchedulerError`` too. A
        terminal failure whose ``error_kind`` names a FlowBlocked subclass
        or a transient transport error (``ConnectionError`` /
        ``TimeoutError``) is re-raised as THAT exception type across the
        seam — the flow-abort contract and transient handling survive the
        ack boundary,

    ``scheduler.submit`` is synchronous, so the future is registered in the
    same event-loop step as the submit — a completion event can never arrive
    between the two (the bus only dispatches across an ``await``).

    RETURN VALUE CONTRACT (documented deviation): the pinned protocol
    declares ``send_* -> DiscordMessage`` but the pipeline only produces a
    human summary string (the DiscordMessage never round-trips the bus).
    ``send_*`` therefore returns ``None`` on success and RAISES on failure —
    plugins treat the calls as fire-and-await-ack. Callers wanting the
    message shape can use ``scheduler.get(task_id)`` / history.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `TaskAckRegistry` | class | yes | task_id -> future map fed by the bus Task* events (one subscription). |
| `MessageWaiterRegistry` | class | yes | Per-account waiter index (FINAL-PHASE F1-b′). |
| `AccountPluginServices` | class | yes | Per-(account, plugin) facade implementing the PluginServices protocol. |

### `TaskAckRegistry`

- `def register(self, task_id: int) -> 'asyncio.Future[Any]'`
  - Create + register the future for ``task_id`` (loop-safe).
- `async def wait_for_task(self, task_id: int, timeout: float) -> Any`
  - Await one task's completion event; returns the TaskCompleted summary.
- `def pending(self) -> List[int]`
  - Task ids currently awaited (introspection/tests).

### `MessageWaiterRegistry`

- `def add(self, account: str, handler: Callable[[MessageReceived], None]) -> Callable[[], None]`
  - Register one waiter; returns the idempotent unsubscribe.
- `def forget_account(self, account: str) -> None`
  - Removal cascade: a re-created same-named account starts clean.
- `def close(self) -> None`
- `def pending(self) -> Dict[str, int]`

### `AccountPluginServices`

- `def account_username(self) -> Optional[str]`
  - The account's own Discord username (None offline/pre-connect).
- `async def send_message(self, channel_id: int, content: str, *, priority: Priority=Priority.LOW) -> None` *(plugin API)*
  - Send to a guild channel through the task pipeline (fire-and-ack).
- `async def send_dm(self, user_id: int, content: str, *, priority: Priority=Priority.LOW) -> None` *(plugin API)*
  - Send a DM through the task pipeline (fire-and-ack; see send_message).
- `async def open_dm(self, user_id: int) -> int` *(plugin API)*
  - Resolve (opening if needed) the DM channel id for a user.
- `async def add_reaction(self, channel_id: int, message_id: int, emoji: str, *, priority: Priority=Priority.NORMAL) -> None` *(plugin API)*
  - React to a message through the paced pipeline (kakerareact).
- `async def click_button(self, channel_id: int, message_id: int, custom_id: str, *, priority: Priority=Priority.NORMAL) -> None` *(plugin API)*
  - Click a message component by custom_id (kakeraloot) — paced,
- `async def fetch_user(self, user_id: int)` *(plugin API)* — side effects: `network`
  - Resolve a user, cached per account in the database.
- `async def cooldown_set(self, key: str, duration_s: float, *, source: str='plugin', reason: str='', metadata: Optional[Dict[str, Any]]=None) -> Dict[str, Any]`
  - Start (or replace) a cooldown for THIS (account, plugin) pair.
- `async def cooldown_remaining(self, key: str) -> float`
  - Seconds left on a cooldown (0.0 when none/expired).
- `async def cooldown_active(self, key: str) -> bool`
  - True when a cooldown is currently active for this key.
- `async def cooldown_clear(self, key: str) -> bool`
  - Remove one cooldown (memory + database); True when it existed.
- `async def cooldown_list(self) -> List[Dict[str, Any]]`
  - Every ACTIVE cooldown of this (account, plugin) pair, soonest
- `async def state_get(self, key: str, default: Any=None) -> Any`
  - Read one persisted value from this (account, plugin) scope.
- `async def state_set(self, key: str, value: Any) -> None`
  - Persist one value in this (account, plugin) scope (JSON value).
- `async def state_delete(self, key: str) -> bool`
  - Delete one value from this (account, plugin) scope; True when it
- `async def wait_for_message(self, check: Callable[[MessageReceived], bool], timeout: float) -> Optional[MessageReceived]` *(plugin API)* — side effects: `publishes_event`
  - Wait for ONE matching inbound message; ``None`` on timeout.
- `async def collect_messages(self, check: Callable[[MessageReceived], bool], idle_timeout: float) -> List[MessageReceived]` *(plugin API)*
  - Collect matching messages until ``idle_timeout`` passes unmatched.
- `def publish(self, event: Event) -> None` *(plugin API)* — side effects: `publishes_event`
  - Fire-and-forget publish on the shared bus (safe from sync code in a loop).
- `def validate_channel(self, channel_id: int) -> None` *(plugin API)*
  - Raise :class:`core.errors.TargetGone` when the channel is unusable.
- `def describe_channel(self, channel_id: int) -> str` *(plugin API)*
  - Human-readable channel label (never raises).
- `def credentials(self) -> Any`
  - Fleet credentials (mudae id, owner ids) — never hardcoded.
- `def account(self) -> str`
  - Account name this facade belongs to.
- `def plugin(self) -> str`
  - Plugin name this facade was created for.
- `def settings(self) -> Dict[str, Any]`
  - LIVE resolved settings dict — refreshed in place by the plugin manager.
