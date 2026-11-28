# Client capability adoption — the plugin surface roadmap

**Status:** PLANNED (owner-directed 2026-11-17). Source study:
`knowledgebase/FORK_ENDPOINT_ADOPTION.md` (the official better.py-self
docs, every Tier-1 method verified against the installed fork) +
[mudabot-design-reference.md](mudabot-design-reference.md) — a documented
copy of the two structures this plan measures against (the reactive wait
and the cog–client binding). Mudabot itself is a sibling checkout at
`D:\Workspace\Mudabot`, not part of this repository.

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

## Track B — Mudabot-grade reactive waits ✅ SHIPPED 2026-11-28

**The flaw this track must NOT inherit** (owner review 2026-11-17):
`wait_for` subscribes at call time — a reply arriving between the
paced send's ack and the subscription is MISSED (the event fired to
no listener), and pre-creating the waiter before the send dangles an
irrelevant task and inverts the code's shape. The primitive is
therefore not `wait_for` but **`expect`** — a send-coupled window:

```python
async with ctx.services.expect(
    check=lambda m: m.author_id == MUDAE and m.channel_id == ch and m.mentions_me,
    timeout=30,
) as window:
    await ctx.services.send_message(ch, "$wa")   # INSIDE the window
    reply = await window.match()                 # first match wins
```

Semantics:

* **Entering subscribes BEFORE the send runs** — the window is open
  when the reply's event fires; the race is closed by construction,
  and no plugin-authored task dangles (the framework owns the waiter,
  cancels and reaps it on exit — the live-window discipline).
* **Anchored matching underneath:** the matcher resolves against a
  bounded ring of recent `MessageReceived` events plus the live
  subscription, anchored at window entry — a reply that lands during
  the send's queue wait matches just the same as one landing after.
  (`collect_messages` already needs this ring; one buffer, two
  consumers.)
* **The check sees the frozen DTO** (embeds/components parsed once at
  the boundary) — the expressiveness of Mudabot's raw predicates
  without raw handles.
* Composable waits (multi-message flows) ride the same window:
  `await window.collect(n=2)`, `await window.first_of(check_a,
  check_b)`. Events stay framework-mediated — the window subscribes
  to the bus; it never touches the client's socket.

Shipped 2026-11-28 as `libzentsuite/core/expect.py` (`ExpectWindow`)
plus the `PluginServices.expect` facade verb: entering subscribes
before the send, the bounded ring + consuming cursor anchors matching
at window entry (all events since entry buffered, flood-bounded), and
`match` / `collect(n)` / `first_of` ride the window with one shared
deadline. Checks run at read time and raise `AccountError` to the
awaiting plugin; the framework unwires the waiter at exit.

## Track C — the direct client connection ✅ SHIPPED 2026-11-17

The owner lock shipped the same day it was made: the toolbox carries
the account's library client at attach (bind_client), the contract
docs changed with it (AGENTS §1.4, architecture.rst §2), and the
facade's remaining worth — paced writes, persistence, orchestration
verbs — is recorded in both. The unified-object convention and the
why-not-the-constructor analysis (order + cardinality) live in
archived git history of this plan and the completed record; the
structure lives in the architecture contract.

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
4. Track C (locked): the toolbox carries the account's library
   client at attach — one seam, tests pin the injection and the
   per-account binding (each cog sees ONLY its own account's client).
