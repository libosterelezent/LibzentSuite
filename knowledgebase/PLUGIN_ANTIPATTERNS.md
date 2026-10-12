# Plugin Anti-patterns

> Confirmed-by-architecture rules ONLY — each one is enforced or demanded by the source
> (`core/plugin_base.py`, `core/account_manager/services.py`, `core/plugin_manager/manager.py`,
> `core/task_scheduler/scheduler.py`, `adapters/model.py`, `plugins/soulcopy/plugin.py`).
> API reference: [PLUGIN_API.md](PLUGIN_API.md). Examples: [PLUGIN_EXAMPLES.md](PLUGIN_EXAMPLES.md).

Each rule: **what breaks** (why) + **the correct pattern**.

---

## 1. Never touch the Discord client or adapter directly — services only

**Why it breaks.** The whole safety architecture hangs off one seam: plugins → `ctx.services` →
scheduler → limiter → adapter. `core/plugin_base.py` states it outright: *"Discord I/O goes ONLY
through the PluginServices protocol … plugins never touch a client, a limiter or the bus
directly."* A direct client call skips the 1.5s/action safety floor, per-account lane
serialization, jitter, the audit trail, retry-on-429 and emergency-stop cancellation — the exact
"robotic timing" signals this project exists to avoid. It also breaks the simulator: tests run
against `SimClient`, which a hardcoded client bypass never exercises.

**Correct pattern.** Every outbound action goes through
`ctx.services.send_message / send_dm / add_reaction / click_button / open_dm / fetch_user`;
channel checks through `ctx.services.validate_channel` / `describe_channel`. Priorities, not
sleeps, express urgency.

---

## 2. Never bypass the scheduler — no ad-hoc `asyncio` tasks for sends

**Why it breaks.** An `asyncio.create_task(client.send(...))` (or a raw `httpx` call) has:

* **no pacing** — no rate-limiter grant, no safety floor, no jitter → flag vector;
* **no audit row** — the trail (§ audit) silently misses an outbound action, so post-mortems and
  the pacing checks lie;
* **no cancellation** — `AccountManager.emergency_stop` cancels the account's tasks with
  `scheduler.cancel_matching(account=...)`; a free-floating task survives the stop and can fire
  into a dead session.

**Correct pattern.** Submit through the services facade and await its ack (fire-and-await-ack;
`task_ack_timeout` bounds the wait). If you need concurrent pipeline work, `asyncio.gather` the
*service calls* (as `plugins/soulcopy/plugin.py` gathers `_collect_pages` + `_request_export`) —
never the wire calls themselves.

---

## 3. Never write SQLite (or any persistence) directly

**Why it breaks.** Settings, plugin enabled-state, task history and audit rows are owned by core
managers with invariants: `PluginManager.set_enabled` persists **before** mutating memory;
`SettingsManager.set_plugin_setting` validates → persists → emits `SettingChanged`; the scheduler
writes task history/audit exactly once at terminal state. A plugin writing the DB races those
invariants (e.g. an enabled flag that disagrees with the live runtime) and couples the plugin to
unstable schema internals (`database.*` is INTERNAL CORE API).

**Correct pattern.** Declare a `Setting` and let `set_setting` persist it; read everything else
from `ctx.settings` / events / `services`. If a plugin genuinely needs its own store, that is a
core-level extension discussion, not plugin code.

---

## 4. Never spam logs in error paths — one clean abort line, and never log from logging-adjacent code

**Why it breaks.** Two failure modes confirmed in source:

* *Plugin error paths:* `PluginManager._dispatch` / `_hook` already record `error_count`,
  `last_error` and log the full traceback when a plugin raises. A plugin that also dumps
  tracebacks per failing event multiplies noise by every event × every failure.
* *Logging/audit-adjacent code:* the pipeline's own recorders are deliberately guarded —
  `AuditRecorder` calls are wrapped ("recording must never break the send pipeline"), and
  `TaskScheduler._log` swallows logging failures. Adding unguarded `ctx.logger.exception(...)`
  inside such paths reintroduces the crash loop the guards exist to prevent.

**Correct pattern.** For flow aborts: exactly **one** warning line —
`ctx.logger.warning("flow aborted for %s: %s", ctx.account, flow_block_reason(exc))` — then
return (the soulcopy contract). Anything that *should* be loud (a real bug) may raise once and
let the dispatcher record it. When extending logging/audit-adjacent components, keep the
guarded-swallow discipline.

---

## 5. No raw library objects in persistent state — runtime-only via `unwrap()`

**Why it breaks.** `adapters.model` pins the contract: everything except `raw` is project-owned,
frozen, JSON-describable metadata; `raw` is *"runtime-only (never serialized into the database,
tasks, or caches)"* and `repr=False` so logs never dump it. Persisting a library object leaks
`discord.py-self` internals across restarts, breaks the simulator (sim messages have `raw=None`),
and can drag unpicklable/uncopyable handles into SQLite.

**Correct pattern.** Store ids and plain fields (`event.message_id`, `event.channel_id`,
`event.author_id`, DTO strings/ints). Reach for `message.unwrap()` only inside a single live
code path, and accept it may be `None`.

---

## 6. Never mutate shared event DTOs

**Why it breaks.** One event object fans out to **every** plugin on the account *and* the inbound
audit trail (`AccountManager._audit_inbound` reads `event.content`/`event.message` counts). A
mutation by plugin A changes what plugin B and the audit row see — cross-plugin corruption that
is miserable to debug. Precision: the rich `adapters.model` DTOs are **frozen dataclasses**
(mutation raises `FrozenInstanceError`); the bus event envelopes (`MessageReceived` etc.) are
plain dataclasses — not frozen in code, but equally shared. Treat both as read-only. (Doc/
implementation note: only the rich layer is enforced frozen today.)

**Correct pattern.** Copy what you need (`dict(embed.field_map())`, `list(message.buttons)`,
`event.content.strip()`); derive, don't edit. If state must change, publish your own `Event`
subclass via `ctx.services.publish(...)`.

---

## 7. Never swallow `FlowBlocked` silently — and never re-raise it out of `handle_event`

**Why it breaks.** Two symmetric failure modes, both called out in
`core/plugin_base.py` and `plugins/soulcopy/plugin.py`:

* *Swallowing silently* (bare `except FlowBlocked: pass`): the flow dies with no evidence — the
  operator cannot tell a dead channel from a broken plugin.
* *Re-raising*: the dispatcher's isolation turns it into `error_count += 1` +
  `last_error = "MessageReceived: TargetGone: ..."` and a traceback log — the flow is over either
  way, but now it looks like a crash and pollutes the health signal (`states()`).

**Correct pattern.** Wrap the **whole** multi-step flow in one `try`, catch `FlowBlocked`, log
**one** clean abort line with `exc.reason`, return:

```python
try:
    await self._run_copy(ctx, event.channel_id)
except FlowBlocked as exc:
    ctx.logger.warning("flow aborted for %s: %s", ctx.account, exc.reason)
```

No partial output: validate targets up-front (`validate_channel`) so an abort can never leave
half a flow behind.

---

## 8. Don't hold `ctx` snapshots across awaits for settings — the live dict is the truth

**Why it breaks.** `ctx.settings` is the **live** dict the manager mutates in place
(`PluginManager.set_setting` → `runtime.settings[key] = validated`), precisely so long-lived
instances see changes without rebuilds. Copying it once (`snapshot = dict(ctx.settings)`) before a
long flow (soulcopy flows run minutes) freezes stale values; caching the whole `ctx` across
activations is worse — a fresh context is built per dispatch, and the services facade is rebuilt
per account start.

**Correct pattern.** Keep flows short; read `ctx.settings["..."]` at each use site (as both
shipped plugins do inside loops). If a flow spans many awaits and the setting is
flow-consistency-critical, read it once **at flow start** and accept the documented staleness —
never at module/import time.

---

## 9. Owner-gate every trigger

**Why it breaks.** Any Discord user can type a word in a channel the account can see; without a
gate the account reacts to strangers — the fastest way to get reported and banned. Both shipped
plugins gate with `ctx.services.credentials.owner_user_ids` (a `frozenset[int]` — the injected
credentials view `AccountPluginServices.credentials` returns, seeded from `.env`
`OWNER_USER_IDS`; the DB-first admin list lives in `AccessManager`), and the simulator's
`control_mention` scenario explicitly verifies that a
non-owner trigger produces **nothing**.

**Correct pattern.**

```python
if event.author_id not in ctx.services.credentials.owner_user_ids:
    ctx.logger.warning("ignoring trigger from non-owner %s ...", event.author_id)
    return
```

Apply to every entry point — message triggers, `ReactionAdded` handlers, mentions. The Mudae bot
id is likewise `ctx.services.credentials.mudae_bot_id`, never hardcoded.

---

## Quick checklist (all rules, one line each)

1. Services only — never a client, limiter or bus handle.
2. Pipeline only — no ad-hoc `asyncio` sends (no pacing/audit/cancel).
3. No direct database writes.
4. One clean abort line; no log spam; guarded logging-adjacent code.
5. `raw`/`unwrap()` is runtime-only; persist ids and plain fields.
6. Events and rich DTOs are read-only shared state.
7. Catch `FlowBlocked` once, log once, return — never re-raise for flow aborts.
8. Re-read `ctx.settings` at use time; don't snapshot across long awaits.
9. Owner-gate every trigger with `credentials.owner_user_ids`.
