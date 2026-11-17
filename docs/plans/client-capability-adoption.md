# Client capability adoption — the plugin surface roadmap

**Status:** PLANNED (owner-directed 2026-11-17). Source study:
`knowledgebase/FORK_ENDPOINT_ADOPTION.md` (the official better.py-self
docs, every Tier-1 method verified against the installed fork) +
`/home/libosterelezent/Workspace/Mudabot/` — the singleton-account
reference whose cog model this plan measures us against.

## The gap, stated honestly (the Mudabot fact)

Mudabot's `MyBot(commands.Bot)` carries the app's own state AS client
attributes (`prefixrate`, `task_manager`, `permits`), and every cog
receives the WHOLE client: `self.bot.wait_for('message',
check=<predicate over raw embeds>)` is the engine of every game flow
(19 uses across the cogs), with `self.bot.user`, `get_channel`, and
the hung services one attribute away. The client object IS the
service locator.

LibzentSuite's plugin surface — `ctx.services` (paced sends,
cooldowns, state, profiles) + the CogServices toolbox — is a deliberate
keyhole: safe, audited, multi-account-isolated… and next to that
pattern it reads as SHALLOW. The facade verbs cover send/react/wait,
but not the reading surface, the message lifecycle, or the
predicate-rich reactivity Mudabot's game logic actually runs on.

This plan closes that depth gap WITHOUT abandoning the middleware
(security lives in mediation, not in poverty of surface).

## Track A — Tier-1 endpoints through the facade (paced, audited)

| New facade verb | Fork surface it wraps |
|---|---|
| `search_messages(query, *, channel/guild, limit)` | `TextChannel/DMChannel.search` — server-side search on user accounts (bot docs don't even have it) |
| `channel_history(channel_id, limit, before/after)` | `Channel.history` |
| `channel_pins(channel_id)` | `Channel.pins` |
| `edit_message(message_ref, content)` / `delete_message(message_ref)` | `Message.edit` / `Message.delete` |
| `reply_message(message_ref, content)` / `forward_message(message_ref, channel_id)` | `Message.reply` / `Message.forward` |
| `ack_message(message_ref)` / `ack_channel(channel_id)` | `Message.ack` / `Client.bulk_ack` — read-state realism |
| `remove_reaction(message_ref, emoji)` | `Message.remove_reaction` |
| `fetch_user_profile(user_id)` / `mutual_friends(user_id)` | `User.profile` / `User.fetch_mutual_friends` |

Reads ride the account-scoped cache (a read pacing class, not the
send lane); writes ride the EXISTING paced pipeline unchanged.

## Track B — Mudabot-grade reactive waits

The facade grows the predicate wait Mudabot's game logic runs on,
over our frozen DTO (embeds/components already parsed once at the
boundary — the check sees what Mudabot's check sees, minus raw
handles):

```python
msg = await ctx.services.wait_for(
    check=lambda m: m.author_id == MUDAE and m.has_embed_with("$wa"),
    timeout=30,
)
```

`wait_for_message`/`collect_messages` today take structured filters;
`wait_for(check, timeout=)` with arbitrary predicates over
`DiscordMessage` is the expressiveness parity. Events stay
framework-meditated (the bus already publishes typed events — the
wait subscribes, it never touches the client's socket).

## Track C — the client view (DECISION POINT, owner to lock)

The Mudabot pattern's real power: cogs reach EVERYTHING on the
client. The scoped compromise: inject a **read-only ClientView** into
the toolbox — `client.user`, `get_channel`, `channels`/`guilds` cache
snapshots, `fetch_user`, plus the Track-A reads as passthroughs —
with every WRITE path absent and the view's provenance
(account-bound, generation-fenced) on the object.

- **For:** plugin authors stop hitting the keyhole; game packs read
  guild/channel state naturally; parity with the reference script.
- **Against:** relaxes AGENTS §1.4's absolute "plugins never touch
  clients" into "never touch UNMEDIATED WRITES" — the cache becomes
  shared state plugins can walk (reads have no audit trail).
- **The rule that holds either way:** writes (messages, reactions,
  friendships, presence, identity) NEVER bypass the paced pipeline.

## Tier 2 (behind explicit policy gates) and Tier 3 (excluded)

Carried verbatim from the knowledgebase study: typing staging,
presence/activities, friend/block actions (never auto-friend between
workspace members without operator policy), group DMs, threads —
gated. Identity mutations, bulk deletes, payments, guild join/leave,
voice — excluded, documented.

## Sequencing proposal

1. Track B first (small, unblocks game logic expressiveness today).
2. Track A reads (search/history/pins) — the parser-pack engine.
3. Track A lifecycle (edit/delete/reply/ack).
4. Track C decided by owner lock; if adopted, ships as its own phase
   with an isolation test matrix (reads scoped, writes impossible).
