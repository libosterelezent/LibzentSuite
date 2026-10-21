# Plugin API Reference

> **THE formal API reference for plugin authors.** Every name here is verified against source:
> `core/plugin_base.py`, `core/account_manager/services.py`, `core/account_manager/cache.py`,
> `core/task_scheduler/scheduler.py`, `core/event_bus.py`, `core/settings_manager.py`,
> `core/priority.py`, `core/errors.py`, `adapters/model.py`.
> System-level context: [PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md). Hard rules: [PLUGIN_ANTIPATTERNS.md](PLUGIN_ANTIPATTERNS.md).

---

## 1. Stability tiers

| Tier | Contents | Contract |
|---|---|---|
| **PUBLIC PLUGIN API** — everything a plugin may depend on | `core.plugin_base.PluginBase` (metadata ClassVars + `on_load`/`on_enable`/`on_disable`/`on_unload`/`handle_event` + `setting_defaults()`), `core.plugin_base.PluginContext` (fields below), the `PluginServices` surface as implemented by `core.account_manager.services.AccountPluginServices`: `send_message`, `send_dm`, `open_dm`, `fetch_user`, `add_reaction`, `click_button`, `wait_for_message`, `collect_messages`, `publish`, `validate_channel`, `describe_channel`, properties `credentials`/`account`/`plugin`/`settings`, attribute `task_ack_timeout`; `core.settings_manager.Setting`; `core.priority.Priority`; the event DTOs plugins receive (`core.event_bus.MessageReceived`/`MessageEdited`/`MessageDeleted`/`ReactionAdded` carrying `adapters.model.DiscordMessage`); `core.errors.FlowBlocked` family (`FlowBlocked`, `CacheInvalid`, `TargetGone`, `AccessDenied`, `FetchRequired`), `SchedulerError`, `AccountError` | Additive changes only. New event types, new rich-DTO fields and new services methods arrive with defaults; existing signatures keep their meaning. Plugins must tolerate additive fields (e.g. `event.message` may gain data) and must not depend on `None`-return internals beyond what is pinned here. |
| **INTERNAL CORE API** — plugins must **not** call | `core.api.ControlAPI` (the interfaces' surface), `core.account_manager.AccountManager` (incl. `send_hook`, `emergency_stop`), `core.plugin_manager.PluginManager`, `core.task_scheduler.TaskScheduler` (never submit directly), `core.rate_limit_manager.*`, `database.*` repositories, `core.audit.AuditRecorder`, `core.logging_service.*`, `core.bootstrap` | No stability promise across releases; reaching into these bypasses pacing, audit and error isolation (see [PLUGIN_ANTIPATTERNS.md](PLUGIN_ANTIPATTERNS.md)). |
| **PRIVATE** | any identifier starting with `_` (e.g. `AccountPluginServices._acks`, `PluginManager._dispatch`, `adapters.model._text`) | Not part of any contract; may change without notice. |

Note on the protocol vs the implementation: `core.plugin_base.PluginServices` (the pinned
`typing.Protocol`) declares the send/wait/publish/validate surface; `AccountPluginServices`
**also** implements `add_reaction`, `click_button` and `fetch_user`, which the protocol class
does not (yet) declare. They are documented here as PUBLIC because they are the only sanctioned
path for those actions — see §12 (Discrepancies) for the typing gap.

---

## 2. `PluginBase` — the plugin contract (`core.plugin_base.PluginBase`)

```python
class MyPlugin(PluginBase):
    name: ClassVar[str] = "myplugin"          # REQUIRED: lowercase [a-z0-9_-]+, unique
    version: ClassVar[str] = "1.0.0"          # display only
    description: ClassVar[str] = "..."        # one line for CLI/dashboard listings
    settings: ClassVar[Sequence[Setting]] = ( # manifest; resolved per account
        Setting("trigger", "str", "go", "Command word (owners only)."),
    )
```

Hooks (all async, all default no-ops — omit what you don't need):

| Hook | Signature | Called |
|---|---|---|
| `on_load` | `async def on_load(self, ctx: PluginContext) -> None` | once, first enable on an account |
| `on_enable` | `async def on_enable(self, ctx: PluginContext) -> None` | every activation (after `on_load`) |
| `on_disable` | `async def on_disable(self, ctx: PluginContext) -> None` | disable / account stop |
| `on_unload` | `async def on_unload(self, ctx: PluginContext) -> None` | removal from the account (last call) |
| `handle_event` | `async def handle_event(self, ctx: PluginContext, event: Event) -> None` | every routed Discord event for this account (§4) |

Helper: `MyPlugin.setting_defaults() -> Dict[str, Any]` (manifest defaults as a plain dict).

`handle_event` **must filter fast** (event type + trigger check before anything async): the
manager routes every inbound message to every enabled plugin, so a slow prologue is multiplied by
the plugin count.

---

## 3. `PluginContext` — everything a plugin may touch (`core.plugin_base.PluginContext`)

| Field | Type | Meaning |
|---|---|---|
| `account` | `str` | account name this instance runs as |
| `plugin` | `str` | plugin name |
| `settings` | `Dict[str, Any]` | **live** resolved settings — `PluginManager.set_setting` mutates this dict in place; re-read values at use time instead of caching them across long awaits |
| `services` | `PluginServices` | per-(account, plugin) I/O facade (§5–§11) + persistence (§14–§15) |
| `logger` | `logging.Logger` | pre-scoped `libzent.<account>.<plugin>`; stdlib call style `ctx.logger.info("fmt %s", arg)` |

A fresh context is built per hook invocation and per event dispatch; the *settings dict* is the
shared live object, the context itself is cheap.

---

## 4. Events plugins receive

`PluginManager` subscribes to exactly four bus event types; `handle_event` receives those and
nothing else (framework events like `TaskCompleted`/`SettingChanged` are **not** routed to
plugins). All are `core.event_bus.Event` dataclasses (`account: str`, `ts: float`).

| Event | Fields | Rich payload |
|---|---|---|
| `MessageReceived` | `message_id: int`, `channel_id: int`, `author_id: int`, `content: str`, `dm: bool` | `message: Any` — an `adapters.model.DiscordMessage` (or `None` for synthetic/legacy events; always guard) |
| `MessageEdited` | `message_id`, `channel_id`, `guild_id: Optional[int]` | `message` — rich **post-edit** `DiscordMessage` (`raw` = library message) |
| `MessageDeleted` | `message_id`, `channel_id`, `guild_id` | none (the gateway gives no body) |
| `ReactionAdded` | `message_id`, `channel_id`, `guild_id`, `user_id: int`, `emoji: str` (printable), `dm: bool` | `raw: Any` (`repr=False`) — runtime-only library handle pair |

`MessageReceived` is emitted with **no message fetch**: everything arrives from what the client
already held. Own sends never come back as events (the adapter self-filters).

### 4.1 The rich DTO — `adapters.model.DiscordMessage` (frozen, `repr=False`)

Compat fields (exact original order): `id: int`, `channel_id: int`, `author_id: int`,
`content: str`, `dm: bool`. Rich additions (all defaulted, all project-owned and frozen):

| Field | Type | Notes |
|---|---|---|
| `guild_id` | `Optional[int]` | |
| `author` | `Optional[DiscordUser]` | `id`, `name`, `display_name`, `bot` |
| `channel` | `Optional[DiscordChannel]` | `id`, `name`, `type`, `guild_id` |
| `embeds` | `Tuple[DiscordEmbed, ...]` | title/description/url/type/color/timestamp/author_*/footer_*/`fields` (`DiscordEmbedField(name, value, inline)`)/thumbnail/image |
| `components` | `Tuple[DiscordComponent, ...]` | top-level action rows; see below |
| `attachments` | `Tuple[DiscordAttachment, ...]` | `id`, `filename`, `url`, `size`, `content_type` |
| `reactions` | `Tuple[DiscordReaction, ...]` | aggregate state on the message: `emoji`, `name`, `id`, `count`, `me` (per-user events are `ReactionAdded`) |
| `reference` | `Optional[DiscordMessageReference]` | ids only + `referenced` **only** when the library already cached it — never fetched |
| `created_at` | `Optional[datetime]` | |
| `raw` | `Optional[Any]` (`repr=False`) | runtime-only escape hatch — `unwrap()` |

Helpers on the DTO:

* `message.buttons -> Tuple[DiscordComponent, ...]` — every BUTTON in the tree (kakeraloot convenience).
* `message.button(custom_id) -> Optional[DiscordComponent]` — find a button anywhere in the tree.
* `message.unwrap() -> Any` — the original library object. Contract: **runtime-only** (never
  serialize/persist), library-specific (couples you to `discord.py-self`), optional (`None` for
  simulator-built messages).
* `message.summary() -> str` — compact metadata one-liner (`id=... ch=... g=... author=... dm
  embeds=2 components=1 ...`), no payloads — safe for logs/audit.
* `DiscordComponent`: `type: int`, `type_name` (`"action_row"|"button"|"select"|"text_input"|"unknown"`),
  `custom_id: Optional[str]`, `label`, `style`, `disabled`, `emoji`, `url`,
  `options` (`(label, value, description, default)` tuples), nested `components`;
  `.walk()` (whole tree, depth-first, self included) and `.find(custom_id)`.
* `DiscordEmbed.field_map() -> dict` — `{name: value}` for roster-style parsing; `.colour` alias.

Treat every DTO as **read-only shared state** (see [PLUGIN_ANTIPATTERNS.md](PLUGIN_ANTIPATTERNS.md)).

---

## 5. `Setting` and `Priority`

### `core.settings_manager.Setting` (frozen dataclass)

```python
Setting(name, type="str", default=None, description="", min_value=None, max_value=None)
```

`type` ∈ `("str", "int", "float", "bool")`; `min_value`/`max_value` only for numeric types; the
default is validated at construction. `validate(value)` coerces + bounds-checks and raises
`core.errors.SettingsValidationError(path, reason)`. Full layering/resolution story:
[PLUGIN_SYSTEM.md §6](PLUGIN_SYSTEM.md).

### `core.priority.Priority` (IntEnum)

`HIGH = 0`, `NORMAL = 1`, `LOW = 2` — **lower value grants earlier**. Services defaults:
`send_message`/`send_dm` → `Priority.LOW`; `add_reaction`/`click_button` → `Priority.NORMAL`.
`Priority.coerce(value)` accepts an int/name-string/`None` (→ `NORMAL`).

---

## 6. `send_message` — guild-channel send

```python
await ctx.services.send_message(channel_id, content, *, priority=Priority.LOW) -> None
```

* **Purpose** — deliver one message to a guild channel through the paced, audited task pipeline.
* **Why** — every outbound action must be paced (safety floor, per-account lanes, jitter),
  auditable and cancellable (emergency stop). The facade is the only path that guarantees all
  three; plugins never sleep to self-throttle.
* **Inputs**

| Parameter | Type | Meaning |
|---|---|---|
| `channel_id` | `int` | target channel (coerced with `int()`) |
| `content` | `str` | message text |
| `priority` | `Priority` | queue priority (default `LOW`) |

* **Returns** — **`None` on success.** This is a documented deviation from the pinned protocol
  annotation (`-> DiscordMessage`): the pipeline only produces a human summary string (the
  `DiscordMessage` never round-trips the bus). Treat the call as **fire-and-await-ack**: it
  returns when the task reached a terminal state (§11). Callers wanting the message shape can use
  the internal `scheduler.get(task_id)` / task history — internal API, not for plugins.
* **Expected result** — the message is delivered (or fails); the call resolves after the task's
  terminal event.
* **Side effects** — `TaskScheduler.submit(account, plugin, "message", channel_id=..., content=...,
  priority=...)` → per-account lane serialization → rate-limiter grant (pacing wait + jitter) →
  optional typing indicator (`typing_probability` account setting, default 0.0) →
  `AccountManager.send_hook` → `cache.validate_channel` → client send. Exactly one outbound audit
  row at task-terminal (action `"message"`, `direction "out"`, `note "plugin=<name> task=<id>
  [attempts=N]"`, `wait_ms`/`jitter_ms` from the grant) + one task-history row.
* **Errors**
  * `SchedulerError` — the task failed terminally, was cancelled, exceeded `task_ack_timeout`, or
    `submit` rejected it (e.g. scheduler stopped after a global emergency stop).
  * `FlowBlocked` subclasses (`TargetGone`, `AccessDenied`, `CacheInvalid`, `FetchRequired`) —
    reconstructed through the ack seam (§11) so the plugin-side catch actually fires.
  * `ServerRateLimit` and `ConnectionError` are **scheduler-retryable** — the plugin sees them
    only if retries exhaust (then as `SchedulerError`); 429s always earn ≥ 1 retry paced by the
    server's `Retry-After`.
* **Concurrency** — one task per call; the account lane serializes sends per account; the awaited
  future is registered in the same event-loop step as the submit, so a completion event can never
  race the registration.
* **Ownership** — call from the plugin's own flow; do not share one services facade across plugins
  (each `(account, plugin)` pair has its own, and audit/task rows are tagged with `ctx.services.plugin`).

```python
# plugins/roll/plugin.py (verbatim pattern)
ctx.services.validate_channel(event.channel_id)               # BEFORE acting
await ctx.services.send_message(
    event.channel_id, ctx.settings["command"], priority=Priority.NORMAL
)
```

---

## 7. `send_dm` — direct-message send

```python
await ctx.services.send_dm(user_id, content, *, priority=Priority.LOW) -> None
```

* **Purpose / Why / Inputs / Returns** — identical contract to §6 with `user_id: int` instead of
  `channel_id` (action `"dm"`; the DM channel is resolved cache-first by the send hook — see
  [ACCOUNT_SYSTEM.md](ACCOUNT_SYSTEM.md) §DM).
* **Side effects** — additionally, on a cache miss the hook resolves the DM via the three-layer
  lookup (memory → persistent api_cache → **one** `client.open_dm`) and records it in both caches;
  a `TargetGone` on the resolved channel invalidates every cached mapping for that user so the
  next attempt re-opens instead of hammering a dead id.
* **Errors** — as §6. Prefer `send_dm` when you only know the user; prefer `send_message` on the
  channel id you already hold (e.g. one obtained from `open_dm`) to skip re-resolution.
* **Concurrency / Ownership** — as §6.

---

## 8. `open_dm` — resolve (opening if needed) the DM channel id

```python
await ctx.services.open_dm(user_id) -> int
```

* **Purpose** — learn the DM **channel id** for a user (soulcopy uses it to filter collected
  Mudae pages by `channel_id` before any page exists).
* **Why** — anti-flag: DM resolution is cache-first; the REST API is the last resort and its
  result is remembered for the account's lifetime (across restarts via the persistent api_cache).
* **Inputs** — `user_id: int`.
* **Returns** — the DM channel id.
* **Expected result** — a hit in the live memory cache or the account-scoped persistent row;
  otherwise exactly **one** `await client.open_dm(user_id)`, then recorded in both caches.
* **Side effects** — memory cache `record_dm` + persistent `api_cache.set(account, "dm", user_id,
  channel_id)` (persistence failures never break the path).
* **Errors** — `AccountError` on a full miss with **no live client** (account stopped).
* **Concurrency** — pure resolution; safe to call at flow start.
* **Ownership** — the id is account-scoped (each account has its own DM channels with the same user).

```python
# plugins/soulcopy/plugin.py (verbatim)
dm_channel = await ctx.services.open_dm(ctx.services.credentials.mudae_bot_id)
pages = await ctx.services.collect_messages(
    check=lambda e: e.author_id == mudae_id and e.channel_id == dm_channel,
    idle_timeout=ctx.settings["dm_read_timeout"],
)
```

---

## 9. `add_reaction` / `click_button` — capability actions

```python
await ctx.services.add_reaction(channel_id, message_id, emoji, *, priority=Priority.NORMAL) -> None
await ctx.services.click_button(channel_id, message_id, custom_id, *, priority=Priority.NORMAL) -> None
```

* **Purpose** — react to a message / click a message component by `custom_id` (kakerareact /
  kakeraloot style), with the **same pacing, floor, lane and audit** discipline as a send
  (reaction claim races are exactly where robotic timing gets flagged).
* **Inputs**

| Parameter | Type | Meaning |
|---|---|---|
| `channel_id` | `int` | target channel |
| `message_id` | `int` | target message |
| `emoji` / `custom_id` | `str` | rides in the task `content`; the scheduler treats both opaquely |
| `priority` | `Priority` | default `NORMAL` |

* **Returns** — `None` (fire-and-await-ack like sends; no `DiscordMessage` shape exists for these
  actions).
* **Expected result** — the reaction lands / the component is clicked on the live message.
* **Side effects** — task submit with action `"reaction"`/`"button"`; the send hook validates the
  channel then delegates to the client capability method, which resolves the **live message on
  demand** (cache first, fetch on miss — the one plugin-demanded fetch these actions perform).
  Audited like a send (both actions are in the scheduler's audited set).
* **Errors** — as §6, plus `TargetGone` when the `custom_id` is no longer on the live message
  (raised by the adapter with the id in the reason).
* **Concurrency** — serialize claim-vs-claim races by keeping one flow per message; the per-account
  lane already serializes the wire calls.
* **Ownership** — discover `custom_id`s from the rich DTO (`event.message.button(custom_id)` /
  `.buttons`), never by re-fetching.

```python
rich = event.message                       # may be None for synthetic events
target = rich.button(ctx.settings["button_id"]) if rich is not None else None
if target is not None:
    await ctx.services.click_button(
        event.channel_id, event.message_id, target.custom_id, priority=Priority.NORMAL
    )
```

---

## 10. `fetch_user` — DB-cached user resolution

```python
await ctx.services.fetch_user(user_id) -> SimpleNamespace(id, name) | Any | None
```

* **Purpose** — resolve a user's display name without an API call when possible.
* **Why** — the framework's fetch-avoidance principle: entities resolve from the account's
  persistent `api_cache` ("user" row) first; the client fetch happens once and is cached forever
  for that account.
* **Inputs** — `user_id: int`.
* **Returns** — a small namespace with `id`/`name` from the cache, the raw client user object when
  no repository is wired, or `None` when the client cannot resolve the user (including no live
  client).
* **Side effects** — on a cache miss with a live client: one `client.fetch_user`, then a
  best-effort `api_cache.set` (failures swallowed).
* **Errors** — none raised for unknown users (`None`); adapter-mapped errors propagate from the
  underlying fetch.
* **Concurrency / Ownership** — account-scoped cache; the returned object is a plain namespace —
  safe to keep, cheap to re-fetch.

---

## 11. Scheduler-backed ack semantics (all pipeline actions)

Every pipeline method (`send_message`, `send_dm`, `add_reaction`, `click_button`) follows the same
fire-and-await-ack contract, implemented by `core.account_manager.services.TaskAckRegistry`:

1. `scheduler.submit(...)` is **synchronous** — the task exists and is queued the moment it returns.
2. `send_*` registers an `asyncio.Future` under `task.id` **in the same event-loop step** (the bus
   only dispatches across an `await`, so a completion event can never arrive between submit and
   registration).
3. The registry holds ONE subscription per process to `TaskCompleted` / `TaskFailed` /
   `TaskCancelled`:
   * `TaskCompleted` → resolves with the event's `summary` string (the service returns `None` —
     the summary is not the message);
   * `TaskFailed` → if `error_kind` (the exception class name carried on the event) is one of the
     FlowBlocked family (`FlowBlocked`, `CacheInvalid`, `TargetGone`, `AccessDenied`,
     `FetchRequired`), the **original exception class is reconstructed** with the reason unwrapped
     from the scheduler's error string — so the plugin catches `TargetGone`, not an opaque
     `SchedulerError`. Everything else raises
     `SchedulerError(f"task {id} failed: {error}")`;
   * `TaskCancelled` → `SchedulerError(f"task {id} was cancelled")`.
4. The wait is bounded by **`ctx.services.task_ack_timeout: float` (default `30.0` seconds)** — a
   task that never reaches a terminal state raises
   `SchedulerError("task <id> did not complete within <N>s")`.

Pipeline side effects common to all four actions: per-account lane serialization, rate-limiter
grant (recorded on the task as `grant_wait_ms`/`grant_jitter_ms`), retry classification
(`ServerRateLimit` always earns ≥ 1 retry after the server's wait; `ConnectionError`/timeouts
retry with exponential backoff; `FlowBlocked` is terminal immediately), exactly-once terminal
audit row + task-history row, and emergency-stop cancellability (`scheduler.cancel_matching`).

---

## 12. `wait_for_message` / `collect_messages` — inbound matching

```python
await ctx.services.wait_for_message(check, timeout) -> Optional[MessageReceived]
await ctx.services.collect_messages(check, idle_timeout) -> List[MessageReceived]
```

### `wait_for_message`

* **Purpose** — wait for **one** matching inbound message; `None` on timeout.
* **Why** — question/answer flows (send a command, await the reply) without polling.
* **Inputs** — `check: Callable[[MessageReceived], bool]`; `timeout: float` (**must** be > 0,
  else `AccountError`).
* **Returns / Expected result** — the first matching `MessageReceived` (fast-return on the first
  match — it does not sit out the window), or `None` after `timeout` seconds. Only events of this
  account are considered.
* **Side effects** — a temporary bus subscription (always unsubscribed, even on timeout).
* **Errors** — `AccountError` when `check` raises (the cause is chained, not swallowed into the
  bus logger); `AccountError` for a non-positive timeout.
* **Concurrency** — one subscription per call; safe to run concurrently with sends.
* **Ownership** — keep `check` pure and fast (it runs inside the bus handler for every inbound
  message of the account).

### `collect_messages`

* **Purpose** — collect matching messages until `idle_timeout` passes with **no new match**
  (ported container semantics).
* **Why** — multi-page replies (soulcopy's DM pages): a chatty sender keeps the collector alive,
  a quiet one ends it.
* **Inputs** — `check: Callable[[MessageReceived], bool]`; `idle_timeout: float` (> 0 else
  `AccountError`).
* **Returns / Expected result** — the list of matched events in arrival order; every match
  **resets** the idle window.
* **Side effects / Errors / Concurrency / Ownership** — as `wait_for_message`.

---

## 13. `publish` / `validate_channel` / `describe_channel`

### `publish(event) -> None` (sync)

* **Purpose** — fire-and-forget publish of an `Event` (typically your own `Event` subclass) on
  the shared bus; safe to call from sync code inside a loop. Handler isolation is the bus's job.

### `validate_channel(channel_id) -> None` (sync)

* **Purpose** — raise `core.errors.TargetGone` when the channel is not usable for this account
  (not in the account's cache: deleted, evicted, or never visible).
* **Why / When** — call it **before** any multi-step flow sends anything: a gone channel then
  aborts the flow before a single message leaves the account (no partial output). This is the
  flow-safety seam the whole abort contract hangs on.
* **Errors** — `TargetGone(account, reason)` — a `FlowBlocked` subclass; catch `FlowBlocked` and
  log one abort line.

### `describe_channel(channel_id) -> str` (sync)

* Human-readable label: `"#name in guild"` / `"dm with <user>"` / `"channel-<id>"` fallback.
  **Never raises** — safe in log lines.

---

## 14. Persistent cooldowns — `cooldown_set` / `cooldown_remaining` / `cooldown_active` / `cooldown_clear` / `cooldown_list`

```python
row = await ctx.services.cooldown_set(key, duration_s, *, source="plugin",
                                      reason="", metadata=None) -> Dict[str, Any]
seconds_left = await ctx.services.cooldown_remaining(key) -> float
active = await ctx.services.cooldown_active(key) -> bool
existed = await ctx.services.cooldown_clear(key) -> bool
rows = await ctx.services.cooldown_list() -> List[Dict[str, Any]]
```

* **Purpose** — persistent, account-scoped cooldown windows (roulette waits,
  claim lockouts, self-imposed pauses) that **survive restarts with the
  REMAINING time** (absolute-epoch expiry: 45 min set → framework down
  10 min → restart → ~35 min left, never a fresh 45).
* **Why** — the framework owns persistence, lookup, expiry and cleanup
  (one `cooldowns` table, boot-time `restore()` + expired-row sweep);
  plugins own WHAT a cooldown means and WHEN to create/clear it (spec §22).
  Plugins never write SQL, never `time.sleep`, never store deadline
  timestamps in their own state.
* **Scope** — every method is scoped to THIS `(account, plugin)` pair:
  account A can never observe account B's cooldowns, and two plugins on
  one account never collide on a key.
* **Inputs**

| Parameter | Type | Meaning |
|---|---|---|
| `key` | `str` | cooldown name, unique per (account, plugin) — e.g. `"roulette"` |
| `duration_s` | `float` | seconds from NOW (re-set replaces the window) |
| `source` | `str` | provenance tag for the dashboard (`"plugin"` default; core uses e.g. `"discord_message"`) |
| `reason` | `str` | human-readable note |
| `metadata` | `Optional[Dict]` | JSON-safe payload kept with the row |

* **Returns** — `cooldown_set` returns the stored row
  (`account`/`plugin`/`cooldown_key`/`started_at`/`expires_at`/`source`/
  `reason`/`metadata`); `cooldown_remaining` returns seconds left
  (`0.0` when none/expired — never negative); `cooldown_active` is a
  boolean convenience over `remaining > 0`; `cooldown_clear` reports
  whether a row existed; `cooldown_list` returns the pair's ACTIVE
  cooldowns, soonest expiry first.
* **Errors** — `PluginError` when the framework was built without the
  shared CooldownService wired (never in a normal boot).
* **Example**

```python
if await ctx.services.cooldown_active("roulette"):
    return  # still cooling down — do not spam
await ctx.services.cooldown_set("roulette", 45 * 60,
                                reason="claimed", source="plugin")
```

---

## 15. Persistent plugin state — `state_get` / `state_set` / `state_delete`

```python
value = await ctx.services.state_get(key, default=None) -> Any
await ctx.services.state_set(key, value) -> None
existed = await ctx.services.state_delete(key) -> bool
```

* **Purpose** — tiny approved key/value persistence for plugins (streak
  counters, "already reacted to X" markers, migration bookkeeping)
  without plugins touching SQL, connections or table schemas (spec §31).
* **Scope** — the `(account, plugin)` PAIR scope: values are visible to
  THIS plugin on THIS account only (namespaced `pstate:account_plugin:
  <account>/<plugin>:<key>` rows in the `kv` table, JSON-encoded). The
  same PluginStateService also offers global / account / plugin scopes
  internally, but the plugin facade deliberately exposes only the pair
  scope — the least surprising blast radius.
* **Value types** — any JSON-serializable value; round-trips exactly
  (dicts/lists/ints/bools/strings...). Corrupt rows degrade to
  `default` instead of crashing; the next `state_set` heals the row.
* **Errors** — `PluginError` for an empty key or a scope owner containing
  reserved characters, and when the service is unwired.
* **Example**

```python
wins = await ctx.services.state_get("wins", 0)
await ctx.services.state_set("wins", wins + 1)
```

---

## 16. Services properties

| Member | Type | Meaning |
|---|---|---|
| `credentials` | `core.credentials.Credentials` | fleet credentials — `.mudae_bot_id: int` (required int, `.env` `MUDAE_BOT_ID`), `.owner_user_ids: frozenset[int]` (the admin-ids view — DB admin list, env `OWNER_USER_IDS` is only the one-time seed), `.get(key)` / `.require(key)` / `.token_for(account)`. Never hardcode ids. |
| `account` | `str` | the account this facade belongs to |
| `plugin` | `str` | the plugin name the facade was created for |
| `settings` | `Dict[str, Any]` | **live** resolved settings dict — the very object `ctx.settings` refers to; refreshed in place by `PluginManager.set_setting` |
| `cooldown_*` | methods | persistent, restart-correct, pair-scoped cooldowns (§14) |
| `state_*` | methods | persistent pair-scoped key/value state (§15) |
| `task_ack_timeout` | `float` | bounded wait for a task ack (default `30.0`) |

---

## 17. Error vocabulary plugins actually see

| Error (`core.errors`) | Raised at | Plugin-side action |
|---|---|---|
| `FlowBlocked(account, reason)` + subclasses `CacheInvalid`, `TargetGone`, `AccessDenied`, `FetchRequired` | `validate_channel`, pipeline failures reconstructed across the ack seam | catch **`FlowBlocked`**, log ONE abort line (`exc.reason`), return — no re-raise, no partial output |
| `SchedulerError` | pipeline failure/cancel/timeout, submit rejection | treat as flow-level failure; safe to abort the flow with one line |
| `AccountError` | invalid wait timeouts, raising `check`s, DM open with no client | usually a plugin bug — fix the call |
| `ServerRateLimit(retry_after, global_)` | raised by adapters, consumed by the scheduler | normally invisible to plugins (auto-retry); never throttle around it yourself |

---

## 18. Discrepancies (protocol vs implementation)

1. `core.plugin_base.PluginServices` annotates `send_message`/`send_dm` `-> "DiscordMessage"`;
   `AccountPluginServices` returns **`None`** on success (documented deviation in that module's
   docstring). Consequence: `plugins/roll/plugin.py`'s `confirm_reply` branch
   (`if ... and sent is not None:`) is dead code against the real services.
2. The protocol class does not declare `add_reaction` / `click_button` / `fetch_user`, which the
   concrete `AccountPluginServices` implements and which are the only sanctioned paths for those
   actions (documented here as public).
3. The bus event dataclasses are plain (mutable) `@dataclass`es; only the rich
   `adapters.model` DTOs are frozen. Treat both as read-only (shared with the inbound audit trail
   and every other plugin).
