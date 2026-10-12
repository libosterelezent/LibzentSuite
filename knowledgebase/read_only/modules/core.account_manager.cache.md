# Module `core.account_manager.cache`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/account_manager/cache.py`

```
Per-account Discord cache: guilds, channels, DM channels — real-time first.

Ported wholesale from ``git show HEAD:core/discord_cache.py`` (audited through
3 review rounds) onto the rewrite spine, with two additions the rewrite needs:

  * :meth:`DiscordCache.dms` / :meth:`DiscordCache.dm_channel_for` /
    :meth:`DiscordCache.record_dm` — DM channels learned AFTER READY (via
    ``client.open_dm``) are recorded here so DM resolution stays cache-first,
  * :func:`resolve_dm_channel` — the shared cache-first DM resolution used by
    both ``AccountManager.send_hook`` and ``AccountPluginServices.open_dm``.

Principle (user spec): interact with Discord in real time, cache channels and
guilds where needed, RARELY use the fetch API.

Invalidation contract: when cached state goes bad (channel deleted, access
revoked) the cache raises a :class:`core.errors.TargetGone` (a
``FlowBlocked`` subclass) with a human reason — that account's plugin flow
stops while every other account keeps running.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `ChannelInfo` | dataclass | yes | One cached channel (guild channel or DM) as seen at READY. |
| `DiscordCache` | class | yes | In-memory view of one account's Discord world (populated at READY). |

### `DiscordCache`

- `def ingest_snapshot(self, snapshot: Dict[str, Any]) -> None`
  - Replace/merge cache contents from a client ``cached_snapshot``.
- `def dms(self) -> int`
  - Count of live (non-deleted) DM channels — details page metric.
- `def dm_channel_for(self, user_id: int) -> Optional[int]`
  - Cached DM channel id for ``user_id`` (None when not cached).
- `def record_dm(self, channel_id: int, recipient: Optional[int]) -> ChannelInfo`
  - Record a DM channel learned AFTER READY (via ``client.open_dm``).
- `def learn_channel(self, channel_id: int, guild_id: Optional[int], name: str='') -> None`
  - Teach the cache a channel seen in INBOUND traffic.
- `def channel(self, channel_id: int) -> ChannelInfo`
  - Live ChannelInfo or :class:`TargetGone` when unknown/deleted.
- `def describe(self, channel_id: int) -> str`
  - Human label: ``#name in guild`` / ``dm with <user>`` (never raises).
- `def invalidate_channel(self, channel_id: int, reason: str) -> bool`
  - Mark a channel gone; notifies listeners; True when it was known.
- `def on_invalidate(self, callback: Callable[[str, int, str], None]) -> Callable[[], None]`
  - Subscribe an invalidation listener; returns an unsubscribable.
- `def validate_channel(self, channel_id: int) -> ChannelInfo`
  - Validate BEFORE acting: raises :class:`TargetGone` when gone.
- `def snapshot(self) -> Dict[str, Any]`
  - JSON-safe cache summary (manager status / dashboard).

## Functions

- `async def resolve_dm_channel(cache: Optional['DiscordCache'], client: Any, user_id: int, account: str='', cache_repo: Any=None) -> int` — side effects: `db_write, network`
  - Cache-first DM channel resolution (shared by send hook + services).
- `async def invalidate_dm_channel(cache: Optional['DiscordCache'], cache_repo: Any, user_id: int, account: str='') -> None`
  - Drop every cached DM mapping for a user (memory + database).
