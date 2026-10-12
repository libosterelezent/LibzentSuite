# Module `adapters.model`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `adapters/model.py`

```
Rich project-owned Discord message/event model (the adapter boundary).

DESIGN PRINCIPLE (architecture spec): capture the useful information ONCE at
the adapter boundary, preserve it in a stable internal representation, and
keep library-specific details isolated behind an explicit, runtime-only
escape hatch. Plugins consume these DTOs — never ``discord.py-self`` types
for ordinary operations — so future plugins (snipe / kakerareact /
kakeraloot / soulmate farm / component interactions) are additive instead
of requiring another core rewrite.

Stable data vs library-specific objects:
  * everything except ``raw`` is PROJECT-OWNED, frozen (plugins cannot
    corrupt shared event state), JSON-describable metadata;
  * ``raw`` is the optional escape hatch for advanced plugins: the original
    library object, ``repr=False`` (never dumped in logs), runtime-only
    (never serialized into the database, tasks, or caches), accessed via
    ``message.unwrap()`` which documents the contract.

Events carry DATA ONLY (§16): no rate limiting, sleeping, retrying, or
scheduling lives here — actions flow Plugin -> PluginServices -> scheduler
-> limiter -> adapter, exactly like sends.

No blocking I/O anywhere in conversion (§17/§15): the converter reads only
what the library event already holds; missing optionals stay ``None``
rather than triggering fetches.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `DiscordUser` | dataclass | yes | Stable author/user projection (no fetch; what the event held). |
| `DiscordChannel` | dataclass | yes | Stable channel projection (id/name/type; guild via guild_id). |
| `DiscordEmbedField` | dataclass | yes | One embed field (name/value/inline) — the unit kakerareact-style |
| `DiscordEmbed` | dataclass | yes | Structured embed (§11): the commonly useful properties, project-stable. |
| `DiscordComponent` | dataclass | yes | One component (§10): action rows nest their children. |
| `DiscordAttachment` | dataclass | yes | One message attachment (file): id, filename, url, size, content type. |
| `DiscordReaction` | dataclass | yes | Aggregate reaction state as carried ON the message (no user info — |
| `DiscordMessageReference` | dataclass | yes | Reply/reference identifiers (§12): ids only, never a fetch. |
| `DiscordMessage` | dataclass | yes | Rich inbound message DTO — the ONE crossing the adapter boundary. |

### `DiscordEmbed`

- `def colour(self) -> Optional[int]`
  - British spelling alias (mirrors the library's dual naming).
- `def field_map(self) -> dict`
  - {name: value} for quick lookups (roster-style parsing).

### `DiscordComponent`

- `def walk(self) -> Tuple['DiscordComponent', ...]`
  - Every component in the tree, depth-first (self included).
- `def find(self, custom_id: str) -> Optional['DiscordComponent']`
  - Locate a component by custom_id anywhere in the tree.

### `DiscordMessage`

- `def buttons(self) -> Tuple[DiscordComponent, ...]`
  - Every BUTTON component in the tree (kakeraloot convenience).
- `def button(self, custom_id: str) -> Optional[DiscordComponent]`
  - Find a button by custom_id anywhere in the component tree.
- `def unwrap(self) -> Any`
  - Escape hatch (§26): the original library message object.
- `def summary(self) -> str`
  - Compact one-liner for logs/audit: metadata + counts, no payloads.

## Functions

- `def convert_component(component: Any) -> DiscordComponent`
  - Library component -> stable projection (recursive for action rows).
- `def convert_embed(embed: Any) -> DiscordEmbed`
  - Library embed -> stable projection (§11 fields, no deep site data).
- `def convert_user(user: Any) -> DiscordUser`
  - Library user object -> stable DiscordUser projection (no fetch).
- `def convert_channel(channel: Any, *, dm: bool) -> DiscordChannel`
  - Library channel object -> stable DiscordChannel projection.
- `def convert_message(message: Any, *, dm: bool, viewer_id: Optional[int]=None, _depth: int=0) -> DiscordMessage`
  - Library message -> rich DiscordMessage (pure, non-blocking, no fetch).
- `def convert_reaction(*, message_id: int, channel_id: int, guild_id: Optional[int], user_id: int, emoji: Any, dm: bool=False, raw: Any=None) -> dict`
  - Stable reaction payload for the ReactionAdded event (§9).
