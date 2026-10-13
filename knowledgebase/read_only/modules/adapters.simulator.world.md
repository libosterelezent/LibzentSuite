# Module `adapters.simulator.world`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `adapters/simulator/world.py`

```
SimWorld — a complete in-memory Discord world (ported from the audited
``core/simulator.py`` FakeWorld).

Everything here is SYNTHETIC: fixture user/channel ids are module constants
and never correspond to real Discord ids (real ones live in .env only).

The world owns:
  * users / channels maps and a dm flag map,
  * deterministic, SYMMETRIC DM channel ids with a collision fallback
    (audit round 1 fix: two pairs must never merge into one channel),
  * per-channel history plus an append-only journal that survives channel
    deletion (scenario checks need post-mortem evidence),
  * a configurable scripted responder: an exact command string (e.g.
    ``"$slud-s"``) triggers a list of pages delivered via DM to the
    requesting user, one page at a time with a configurable per-page delay,
  * ``wait_for_quiet`` with REAL quiet-window semantics (audit fix: return
    once `quiet` seconds of silence passed, `limit` is only a hard cap),
  * a one-shot server-limit queue (``server_limit_next``): the NEXT matching
    send raises :class:`core.errors.ServerRateLimit` instead of delivering,
    consumed exactly once,
  * ``channel_gone``: remove a channel so later sends raise TargetGone and
    cached snapshots stop listing it.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `WorldMessage` | dataclass | yes | One message that exists in the world (delivered + timestamped). |
| `_PendingLimit` | dataclass | no | A queued one-shot 429 (see SimWorld.server_limit_next). |
| `SimWorld` | class | yes | In-memory Discord: users, channels, DM channels, history, responders. |

### `SimWorld`

- `def add_user(self, user_id: int, name: str) -> None`
  - Register a user id (idempotent; last name wins).
- `def add_channel(self, channel_id: int, name: str, dm: bool=False) -> None`
  - Register a channel id (idempotent; last name wins).
- `def channel_exists(self, channel_id: int) -> bool`
  - True while the channel still exists in the world.
- `def channel_gone(self, channel_id: int) -> bool`
  - Simulate a channel deletion.
- `def dm_channel_for(self, user_a: int, user_b: int) -> int`
  - Deterministic, symmetric DM channel id for a user pair.
- `def peek_dm_channel(self, user_a: int, user_b: int) -> Optional[int]`
  - Non-creating DM channel lookup: the pair's channel id, or None
- `def add_response(self, command: str, pages: List[str]) -> None`
  - Register a scripted response: exact command string -> DM pages.
- `def dm_pairs_of(self, channel_id: int) -> List[Tuple[int, int]]`
  - The (one) user pair behind a DM channel id (per-account filtering).
- `def server_limit_next(self, channel_id: Optional[int]=None, retry_after: float=1.0, global_: bool=False, user_id: Optional[int]=None) -> None`
  - Queue a one-shot server 429 for the NEXT matching send.
- `def take_server_limit(self, user_id: int, channel_id: Optional[int]) -> Optional[ServerRateLimit]`
  - Consume the first matching pending one-shot limit, if any.
- `def deliver(self, channel_id: int, author_id: int, content: str, dm: Optional[bool]=None, *, author_name: str='', guild_id: 'int | None'=None, embeds: tuple=(), components: tuple=(), attachments: tuple=(), reactions: tuple=(), reference_message_id: 'int | None'=None) -> WorldMessage`
  - Append a message to the channel and push it to every subscriber.
- `def react(self, user_id: int, message_id: int, emoji: str, kind: str='add') -> None`
  - Push a reaction event for a known message to every subscriber
- `def edit_message(self, message_id: int, content: str) -> WorldMessage`
  - Rewrite a message's content and push an edit event to subscribers.
- `def delete_message(self, message_id: int) -> WorldMessage`
  - Remove a message and push a delete event (ids only, like live).
- `def cancel_pending_replies(self) -> None`
  - Cancel scripted-responder tasks still in flight (run teardown).
- `def messages_of(self, channel_id: int) -> List[str]`
  - Contents of one channel's history (empty once the channel is gone).
- `def find_delivery(self, *, author_id: int, content: str) -> Optional[WorldMessage]`
  - First delivered message with exactly this author and content.
- `async def wait_for_quiet(self, channel_id: int, quiet: float=0.4, limit: float=10.0) -> None` — side effects: `sleeps`
  - Return once no new messages appear in a channel for `quiet` seconds.

## Functions

- `def to_discord_message(world: 'SimWorld', message: 'WorldMessage')`
  - WorldMessage -> rich frozen DTO (the sim's convert_message).
