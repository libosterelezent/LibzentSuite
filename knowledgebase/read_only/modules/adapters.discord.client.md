# Module `adapters.discord.client`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `adapters/discord/client.py`

```
Real Discord client adapter (ported from the audited core/discord_adapter.py).

Bridges the :class:`adapters.base.DiscordClient` protocol to a live
user-account-capable Discord client library (e.g. ``discord.py-self``).

Ported, audit-proven behaviour:
  * the library import is LAZY (inside :meth:`RealDiscordClient.connect`) so
    tests, simulations and CLI dry-runs never require the library;
  * startup errors FAIL the connect instead of silently yielding a session
    with ``user_id=None`` (audit round 2 fix);
  * a client that reports ready without a user id is treated as a bad token;
  * ``cached_snapshot()`` builds EXACTLY from what the live client already
    holds in cache after READY — never a network fetch.

New in the rewrite:
  * library HTTP errors are translated into the shared domain vocabulary
    (:class:`core.errors.ServerRateLimit` / ``AccessDenied`` / ``TargetGone``)
    so the scheduler can react without knowing the library;
  * ``latency_ms`` and the ``Retry-After`` / ``X-RateLimit-Global`` header
    intake required by the send pipeline (INTEGRATION.md).

NOTE: selfbots violate Discord ToS; the framework keeps pacing conservative.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `RealDiscordClient` | class | yes | DiscordClient protocol implementation over a lazily imported library. |
| `IdentifyGate` | class | yes | Fleet-wide IDENTIFY pacer (Discord's identify limit is per-IP). |
| `RealClientFactory` | class | yes | ClientFactory protocol implementation building live clients. |

### `RealDiscordClient`

- `def account(self) -> str`
  - Best-effort account name (used to enrich AccessDenied reasons).
- `def account(self, name: str) -> None`
- `async def connect(self) -> None` — side effects: `creates_tasks`
  - Import the library, wire callbacks, start the client, await ready.
- `def set_plugin_commands(self, specs: Any) -> None`
  - Adopt a newly published command set (hub → client).
- `def allowed_invoker(self, invoker_id: Optional[int]) -> bool`
  - The gate for one invocation (LEGACY sync view).
- `async def allowed_invoker_async(self, invoker_id: Optional[int]) -> bool`
  - THE command gate: AccessManager.can_invoke (per-account
- `def self_command_allowed(self) -> bool`
  - Account-level view of the gate: is the ACCOUNT'S OWN user id
- `async def disconnect(self) -> None`
  - Close the underlying client; safe when never started.
- `async def send_message(self, channel_id: int, content: str) -> DiscordMessage` — side effects: `network`
  - Send to a channel id (cache first, fetch fallback); returns the
- `async def send_dm(self, user_id: int, content: str) -> DiscordMessage`
  - Send a DM to a user id (same error mapping as send_message).
- `async def open_dm(self, user_id: int) -> int`
  - Open (or reuse) the DM channel with a user; returns its id.
- `async def send_typing(self, channel_id: int) -> None`
  - Fire a typing indicator in a channel (anti-flag human behavior).
- `async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> Any` — side effects: `network`
  - React to a message (kakerareact capability; pipeline-paced).
- `async def click_button(self, channel_id: int, message_id: int, custom_id: str) -> Any`
  - Click a component by custom_id (kakeraloot capability).
- `async def fetch_user(self, user_id: int) -> Any` — side effects: `network`
  - Fetch a user object; None when the user is gone (NotFound).
- `def latency_ms(self) -> Optional[float]`
  - Gateway heartbeat latency in ms (None before READY / when NaN/inf).
- `def cached_snapshot(self) -> dict`
  - In-memory view only — NO network fetches (details page source).
- `def channels_snapshot(self) -> list`
  - Picker-shaped text-channel snapshot — memory only, NO fetches.

### `IdentifyGate`

- `def release(self) -> None`
  - Return one fleet slot (a client's PERMANENT teardown).
- `async def acquire(self, shard_id=None, *, initial: bool=False) -> None` — side effects: `sleeps`
  - Space consecutive IDENTIFYs by ``effective_interval`` seconds.
- `def effective_interval(self) -> float`
  - Current spacing, derived from the expected fleet size.

### `RealClientFactory`


## Functions

