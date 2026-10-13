# Adapters

> One protocol, two implementations: the real Discord client and the offline simulator — plus the
> rich conversion boundary both feed. Source of truth: `adapters/base.py`, `adapters/model.py`,
> `adapters/discord/client.py`, `adapters/simulator/{client,world,runner,scenarios}.py`.
> Consumers: [ACCOUNT_SYSTEM.md](ACCOUNT_SYSTEM.md) · [PLUGIN_API.md](PLUGIN_API.md).

---

## 1. The protocol — `adapters.base.DiscordClient`

The framework (account manager, scheduler sender hook, plugins via services) only ever talks to
this `@runtime_checkable Protocol` — never to a concrete library — which is what makes full
offline simulation possible.

### 1.1 Lifecycle & messaging

| Member | Contract |
|---|---|
| `user_id: Optional[int]` | logged-in Discord user id (`None` until connected) |
| `latency_ms: Optional[float]` | gateway heartbeat latency in ms (`None` when unknown) |
| `connect() / disconnect()` | lifecycle; implementations must be idempotent |
| `send_message(channel_id, content) -> DiscordMessage` | send to a channel id (cache first, fetch fallback) |
| `send_dm(user_id, content) -> DiscordMessage` | send a DM to a user id |
| `open_dm(user_id) -> int` | open (or reuse) the DM channel; returns its id |
| `fetch_user(user_id) -> Any` | fetch a user object |
| `cached_snapshot() -> dict` | **in-memory view only — NEVER a network fetch** (the account details page draws exclusively from this) |

### 1.2 Callbacks (assigned by the account manager BEFORE `connect()`)

| Callback | Required | Signature |
|---|---|---|
| `on_message` | yes | `(DiscordMessage) -> None` — rich DTO |
| `on_disconnect` | yes | `(reason: str) -> None` |
| `on_reaction` | **optional** (rich) | `(kind: "add"\|"remove", message_id, channel_id, guild_id, user_id, emoji: str, raw) -> None` |
| `on_message_edit` | **optional** (rich) | `(DiscordMessage) -> None` — post-edit, `raw` = library message |
| `on_message_delete` | **optional** (rich) | `(message_id, channel_id, guild_id, raw) -> None` |

Optional rich callbacks are wired on **every** client; adapters that never fire them simply leave
the assignments inert (duck-typed).

### 1.3 Capability actions (plugin-demanded, pipeline-paced)

```python
async def add_reaction(channel_id: int, message_id: int, emoji: str) -> Any      # kakerareact
async def click_button(channel_id: int, message_id: int, custom_id: str) -> Any  # kakeraloot
```

Both raise the shared domain errors (`ServerRateLimit` / `AccessDenied` / `TargetGone`);
`click_button` resolves the message on demand.

### 1.4 Server rate-limit contract

When Discord answers 429, the client raises `core.errors.ServerRateLimit(retry_after, global_)`.
The scheduler reports it to the rate-limit manager (`report_server_limit`) and retries after
backoff — the framework always respects server-provided rate-limit information.

Also here: `adapters.base.ClientFactory` — `__call__(name, token) -> DiscordClient` — and the
**compatibility re-exports** of the whole rich model (`DiscordMessage`, `convert_message`, …), so
legacy `from adapters.base import DiscordMessage` keeps working.

---

## 2. The rich conversion boundary — `adapters.model`

Design principle: capture the useful information **once** at the adapter boundary, preserve it in
a stable internal representation, keep library-specific details behind an explicit runtime-only
escape hatch. Plugins consume these DTOs — never `discord.py-self` types for ordinary operations.

### 2.1 Conversion contract (`convert_message(message, *, dm, viewer_id=None)`)

* **One non-blocking conversion** — the converter reads only what the library event already holds;
  nothing is fetched (§15 of the spec). No blocking I/O anywhere in conversion.
* **Missing optionals stay `None`** (explicitly not fetched): absent `author`/`channel`/`guild`/
  `reference`/`created_at` arrive as `None`, empty tuples for collections.
* `dm` is computed by the **caller** (the adapter knows its library's DM channel type);
  `viewer_id` documents whose client saw the event (self-filtering happens in the adapter).
* Everything except `raw` is **project-owned, frozen** (plugins cannot corrupt shared event
  state), JSON-describable metadata.
* `raw` is the escape hatch: the original library object, **`repr=False`** (never dumped in logs),
  **runtime-only** (never serialized into the database, tasks, or caches), accessed via
  `message.unwrap()`.
* Events carry **data only**: no rate limiting, sleeping, retrying or scheduling lives here —
  actions flow plugin → services → scheduler → limiter → adapter, exactly like sends.

Helpers: `DiscordMessage.buttons` / `.button(custom_id)` / `.unwrap()` / `.summary()`;
`DiscordComponent.walk()` / `.find(custom_id)`; `DiscordEmbed.field_map()` / `.colour`.
Companion converters: `convert_component`, `convert_embed`, `convert_user`, `convert_channel`,
`convert_reaction` (returns a plain dict — the core-side event layer must not import adapter
types). Full field tables: [PLUGIN_API.md §4.1](PLUGIN_API.md).

---

## 3. The real adapter — `adapters.discord.client`

### 3.1 Lazy library import & strict startup

`RealDiscordClient.connect()` imports `discord` (the `discord.py-self` fork) **inside connect** —
tests, simulations and CLI dry-runs never require the library (a missing library raises a human
`AccountError` naming the pip package). It wires the library events (`on_ready`, `on_disconnect`,
`on_message`, `on_message_edit`, `on_message_delete`, `on_reaction_add`, `on_reaction_remove`),
starts the client as a background task and awaits READY. Audit fixes kept: startup failures **fail
the connect** (never a silent session with `user_id=None`); ready-without-user-id is treated as a
bad token. `on_message` filters self-messages and performs the one rich conversion
(`convert_message(message, dm=isinstance(message.channel, discord.DMChannel), viewer_id=...)`).

### 3.2 Error mapping (`_map_send_error`)

Library `HTTPException`s are translated into the shared domain vocabulary so the scheduler can
react without knowing the library:

| Library condition | Domain error |
|---|---|
| status 429 (or mapped) | `ServerRateLimit(retry_after, global_)` — `Retry-After` header float-parsed (fallback `1.0`), `X-RateLimit-Global` header → `global_` |
| status 403 / `discord.Forbidden` | `AccessDenied(account, "discord refused a send to channel …: …")` — the account label enriches the reason (set by `RealClientFactory`) |
| status 404 / `discord.NotFound` | `TargetGone("", "channel … is gone: …")` |

Applied to sends, channel resolution, DM opens, reactions and button clicks.

### 3.3 Actions and fetch discipline

* `send_message(channel_id, content)` — channel via `client.get_channel` **then** `fetch_channel`
  fallback; returns a framework-shaped `DiscordMessage` (id/channel_id/author_id/content/dm — dm
  derived from the channel type, so DM sends routed through a resolved channel id still flag
  correctly).
* `send_dm(user_id, content)` / `open_dm(user_id)` — `fetch_user` → `user.dm_channel or
  await user.create_dm()`. (The framework's send hook calls these **once** per user per account
  lifetime and caches the result — see [ACCOUNT_SYSTEM.md §3.3](ACCOUNT_SYSTEM.md).)
* `_resolve_message(channel_id, message_id)` — the ONE plugin-demanded fetch behind
  `add_reaction` / `click_button`: cache first (`channel.get_message`), fetch on miss
  (`fetch_message`). `click_button` walks the live components for the `custom_id` and calls the
  library's `Button.click()`; a vanished id raises `TargetGone` with the id in the reason.
* `send_typing(channel_id)` — best-effort typing indicator; unknown channel → skip rather than
  fetch (API-call trim); failures never propagate (typing must never fail a send).
* `cached_snapshot()` — exactly the live client's post-READY cache: user, guilds + channels,
  `private_channels` as `dm_channels` (recipient ids), latency. `{"available": False}` before
  connect.
* `RealClientFactory(name, token)` — the `ClientFactory` implementation; an empty token falls
  back to the conventional `DISCORD_TOKEN_<NAME>` credential before raising `CredentialsError`.

---

## 4. The simulator — `adapters.simulator`

### 4.1 `SimWorld` (world model)

A complete in-memory Discord world. Everything is **synthetic** (fixture ids are module constants
— `SYNTHETIC_OWNER_ID = 1001`, `SYNTHETIC_ALPHA_ID = 2001`, `SYNTHETIC_BETA_ID = 2002`,
`SYNTHETIC_GUILD_CHANNEL = 500`, `SYNTHETIC_MUDAE_ID = 999_999`; real ids live only in `.env`):

* users / channels maps + a dm flag map; **deterministic, symmetric** DM channel ids with a
  collision-checked fallback (two pairs never merge into one channel);
* per-channel history plus an **append-only journal** that survives channel deletion
  (post-mortem evidence);
* **react** (echoes a `ReactionAdded` to every subscriber except the reactor, incrementing
  aggregate counts on the message), **edit_message**, **delete_message** (delete pushes ids-only
  events, like live);
* a **scripted responder**: an exact command string (e.g. `"$slud-s"`) triggers a list of pages
  delivered via DM to the requesting user, one page at a time with a configurable delay;
* `server_limit_next(...)` — a **one-shot 429 queue**: the next matching send raises
  `ServerRateLimit(retry_after, global_)` instead of delivering; consumed exactly once and
  recorded in `blocked_sends` (with timestamps, for retry-timing checks);
* `channel_gone(channel_id)` — removes a channel so later sends raise `TargetGone` and cached
  snapshots stop listing it (deletion time recorded for scenario post-mortems);
* `wait_for_quiet(channel, quiet, limit)` — **real** quiet-window semantics (returns once
  `quiet` seconds of silence accumulate; `limit` is only a hard cap — ported audit fix);
* `deliver(...)` accepts the full rich payload (embeds/components/attachments/reactions as
  `adapters.model` DTO tuples, author_name, guild_id, reference ids) so scenarios exercise the
  rich boundary exactly like live traffic.

### 4.2 `SimClient` (per account)

Implements the full `DiscordClient` protocol inside a world, so the account manager, scheduler
and plugins cannot tell it apart from the real client:

* idempotent connect/disconnect; `drop(reason)` simulates an **unexpected** loss (fires
  `on_disconnect`); `fail_connect=True` forces connect failures (supervisor testing);
* sends mirror the real error contract: `ConnectionError` when offline, `ServerRateLimit` for a
  queued one-shot 429, `TargetGone` for a vanished channel;
* `add_reaction` goes through the world (echoes events); `click_button` finds the button the
  world still holds (`TargetGone` when the custom_id or message vanished);
* `cached_snapshot()` filters DM channels to pairs involving **this** account (ported audit fix:
  the world-level view used to leak other accounts' DMs);
* `latency_ms` is synthetic but deterministic per seed (40–90 ms);
* `receive(message)` performs the rich conversion into a `DiscordMessage` (author/channel/
  embeds/components/attachments/reactions/reference cross untouched — same contract as live;
  `raw` stays `None`, exercising the "optional" part of the escape hatch);
* observability hooks for tests: `typing_calls`, `open_dm_calls`, `button_clicks`.

### 4.3 `SimulationRunner` — a complete ephemeral stack per run

`SimulationRunner(plugin_dir="plugins", credentials=None).run(name) -> report`. Composition
(pinned constructors):

```
Database(":memory:") + migrate()
EventBus() · LogService()
SettingsManager(bus, logger, plugin_repo, account_settings_repo, global_layer=DEMO_PLUGIN_SETTINGS)
RateLimitManager(bus, logger, db.policies) · AuditRecorder(db.audit)
TaskScheduler(bus, logger, limiter, sender)        <- LATE-BOUND SENDER
PluginManager(bus, logger, settings, db.plugins, plugin_dir) + discover()
ProfileManager(db.profiles, logger, db.accounts)
AccountManager(bus, logger, db.accounts, plugins, profiles, scheduler, settings, audit,
               client_factory(SimClient over the world), credentials[, limiter, api_cache_repo])
```

Notable seams (documented in the module):

* **Late-bound sender**: the scheduler needs `accounts.send_hook`, the account manager needs the
  scheduler — broken with a holder box filled immediately after construction, *before*
  `scheduler.start()`, so no task can run against an unfilled seam.
* **Logger shape**: managers mix stdlib-style and `LogService`-style calls — the runner injects
  `ServiceLogger` adapters over `LogService.record` so both work and structured entries still
  flow to `log_lines` / the Logs page.
* A **real `ControlAPI` + `RemoteControl`** (the retired mention bridge — the
  `control_mention` scenario) are wired so scenarios can exercise Discord-side
  management.
* Scenario policies are **clamped to the 1.5s safety floor** — simulations never configure unsafe
  pacing.
* Observability subscriptions attach **before** any traffic; a temporary `StdlibBridge` feeds
  plugin/loader stdlib lines into the run's log service (a pre-existing bridge is kept —
  documented cross-leak instead).
* Report: `{"name", "passed", "duration_s", "failures", "log_lines", "interactions",
  "summary"}` — scenario crashes become failure strings, never propagated exceptions.

### 4.4 Scenario registry (`adapters.simulator.scenarios.SCENARIOS`)

| Scenario | Verifies |
|---|---|
| `soulcopy_basic` | owner trigger → 2 DM pages → one `$soulcopy ` chunk back in the channel |
| `soulcopy_multichunk` | long roster → several chunks, each ≤ 2000 chars incl. prefix |
| `pacing_respected` | with a 1.6s message interval, every audited outbound gap ≥ interval − 0.05s |
| `server_429_respected` | a simulated 429 (Retry-After 0.8s) is reported, waited out, retried; the flow still completes |
| `channel_gone_aborts` | mid-flow channel deletion aborts cleanly — DM pages arrived but **no chunk is ever sent** |
| `control_mention` | the owner manages the fleet by mentioning the account (`@alpha status`, `estop`); a stranger's mention is completely ignored |

### 4.5 The testing contract

Scenarios run the **same production path** — real `PluginManager`, `AccountManager`,
`TaskScheduler`, `RateLimitManager`, `SettingsManager`, `AuditRecorder`, in-memory `Database` —
with the client seam swapped to `SimClient`. **Zero network**: no library import, no REST calls,
synthetic ids only. Whatever passes here exercises exactly the code that ships.
