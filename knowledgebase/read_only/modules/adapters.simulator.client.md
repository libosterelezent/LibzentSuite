# Module `adapters.simulator.client`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `adapters\simulator\client.py`

```
SimClient — the simulated half of the adapter boundary (ported from the
audited ``core/simulator.py`` FakeClient, adapted to adapters.base).

One SimClient simulates ONE logged-in account living inside a
:class:`adapters.simulator.world.SimWorld`. It implements the full
:class:`adapters.base.DiscordClient` protocol so the account manager,
scheduler and plugins cannot tell it apart from the real client.

Behaviour highlights (audit-proven unless noted):
  * connect/disconnect are idempotent — no duplicate world subscriptions,
  * ``drop(reason)`` simulates an UNEXPECTED loss and fires on_disconnect,
  * ``send_message``/``send_dm`` mirror the real error contract:
    ConnectionError when offline, :class:`core.errors.ServerRateLimit` for a
    queued one-shot 429 (world-owned queue), :class:`core.errors.TargetGone`
    for a vanished channel,
  * ``cached_snapshot`` filters DM channels to pairs involving THIS account
    (audit fix: the world-level view used to leak other accounts' DMs),
  * ``latency_ms`` is synthetic but deterministic per client (seedable).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `SimClient` | class | yes | DiscordClient protocol implementation over a SimWorld (per account). |

### `SimClient`

- `async def connect(self) -> None`
  - Subscribe to the world (idempotent — no duplicate subscriptions).
- `async def disconnect(self) -> None`
  - Unsubscribe from the world (idempotent, never fires callbacks).
- `def drop(self, reason: str='simulated network drop') -> None`
  - Simulate an UNEXPECTED connection loss (fires on_disconnect).
- `def delete_channel(self, channel_id: int) -> bool`
  - Simulate a channel deletion (delegates to the world).
- `async def send_message(self, channel_id: int, content: str) -> DiscordMessage`
  - Deliver a guild-channel message through the world.
- `async def send_dm(self, user_id: int, content: str) -> DiscordMessage`
  - Deliver a DM to a user through the world's deterministic DM channel.
- `async def send_typing(self, channel_id: int) -> None`
  - Record a typing indicator (no world effect; observable in tests).
- `async def open_dm(self, user_id: int) -> int`
  - The (deterministic, symmetric) DM channel id for a user pair.
- `async def fetch_user(self, user_id: int) -> Optional[SimpleNamespace]`
  - None when the world does not know the user, else id/name attrs.
- `def latency_ms(self) -> float`
  - Synthetic heartbeat latency: deterministic 40-90ms per seed.
- `def cached_snapshot(self) -> dict`
  - World-backed equivalent of the live client's in-memory cache.
- `def receive(self, message: WorldMessage) -> None`
  - World push -> framework callback (ignores our own messages).
- `def reaction_event(self, kind: str, message_id: int, channel_id: int, guild_id: 'int | None', user_id: int, emoji: str, raw: Any) -> None`
  - World reaction dispatch -> on_reaction. The world pushes EVERY
- `def can_see(self, channel_id: int) -> bool`
  - Gateway-truth visibility: a client only ever sees channels it
- `async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> Any`
  - React via the world (echoes a ReactionAdded to other subscribers).
- `async def click_button(self, channel_id: int, message_id: int, custom_id: str) -> Any`
  - Click a button the world still holds (TargetGone when vanished).
