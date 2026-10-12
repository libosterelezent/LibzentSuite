# Plugin Examples

> Two complete, **real-API** walkthroughs. Every attribute and call below is verified against
> `core/plugin_base.py`, `core/account_manager/services.py`, `core/event_bus.py`,
> `adapters/model.py`, `core/priority.py`, `core/settings_manager.py`, `core/credentials.py` and
> the test-fixture plugins (`tests/fixtures/plugins/probe/plugin.py`,
> `tests/fixtures/sim_plugins/soulcopy/plugin.py` — the framework itself
> ships with none; the former `plugins/roll` and `plugins/soulcopy`
> packages were removed).
> Formal reference: [PLUGIN_API.md](PLUGIN_API.md). Rules: [PLUGIN_ANTIPATTERNS.md](PLUGIN_ANTIPATTERNS.md).

---

## 1. A complete plugin: `lootclaim`

An owner-triggered component clicker that acknowledges with a reaction, inspects the rich message
DTO, clicks a button, and — above all — obeys the flow-abort contract. Drop it in
`plugins/lootclaim/__init__.py` (or `plugins/lootclaim.py`) and it is discovered on the next scan
(name must match `[a-z0-9_-]+`).

```python
"""lootclaim: acknowledge an owner trigger and click a Mudae component button."""
from __future__ import annotations

from core.errors import FlowBlocked
from core.event_bus import Event, MessageReceived
from core.plugin_base import PluginBase, PluginContext
from core.priority import Priority
from core.settings_manager import Setting


def flow_block_reason(exc: BaseException) -> str:
    """Human-readable reason for a flow abort (pure -> unit-testable).

    Same helper shape as tests/fixtures/sim_plugins/soulcopy/plugin.py: FlowBlocked (and
    subclasses) carry a curated ``reason``; anything else renders generically.
    """
    if isinstance(exc, FlowBlocked):
        return exc.reason
    return f"{type(exc).__name__}: {exc}"


class LootclaimPlugin(PluginBase):
    """React + click on an owner trigger; ignore everything else fast."""

    # ---- metadata + settings manifest (auto-generates BOTH UIs' editors) ----
    name = "lootclaim"                       # lowercase [a-z0-9_-]+, unique
    version = "1.0.0"
    description = "Example plugin: ack a trigger and click a component button."

    settings = (
        Setting("trigger", "str", "loot",
                "Command word that starts the claim (owners only)."),
        Setting("button_id", "str", "claim",
                "custom_id of the button to click."),
        Setting("ack_emoji", "str", "\N{THUMBS UP SIGN}",
                "Emoji used to acknowledge the trigger."),
        Setting("min_embeds", "int", 1,
                "Require at least N embeds before acting.",
                min_value=0, max_value=10),
    )

    # ------------------------------------------------------------ lifecycle
    async def on_load(self, ctx: PluginContext) -> None:
        # runs ONCE when the plugin is first enabled on the account
        ctx.logger.info("lootclaim loaded (trigger=%r)", ctx.settings["trigger"])

    async def on_enable(self, ctx: PluginContext) -> None:
        # runs on every activation (after on_load) — keep it cheap
        ctx.logger.info("lootclaim active on %s", ctx.account)

    async def on_disable(self, ctx: PluginContext) -> None:
        # disabled or the account stops — graceful no-op hook
        ctx.logger.info("lootclaim disabled on %s", ctx.account)

    async def on_unload(self, ctx: PluginContext) -> None:
        # last call before the instance goes away — graceful no-op hook
        ctx.logger.info("lootclaim unloaded")

    # --------------------------------------------------------------- events
    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        """Owner trigger -> one paced flow; everything else ignored FAST."""
        # filter fast: the manager routes EVERY inbound message here
        if not isinstance(event, MessageReceived):
            return
        if event.content.strip() != ctx.settings["trigger"]:
            return
        # owner gate: fleet credentials, never hardcoded ids
        if event.author_id not in ctx.services.credentials.owner_user_ids:
            ctx.logger.warning(
                "ignoring trigger from non-owner %s on account %s (if this is "
                "you, add the id to the admin list in the Admin tab)",
                event.author_id, ctx.account,
            )
            return
        try:
            await self._claim(ctx, event)
        except FlowBlocked as exc:
            # THE contract: one clean abort line, then stop — no re-raise,
            # no partial output. Per-account isolation is the dispatcher's
            # job; failing clean and quiet is the plugin's.
            ctx.logger.warning(
                "flow aborted for %s: %s", ctx.account, flow_block_reason(exc)
            )

    # ------------------------------------------------------------------ core
    async def _claim(self, ctx: PluginContext, event: MessageReceived) -> None:
        """Drive the claim; raises FlowBlocked when the target is unusable."""
        # validate BEFORE acting so a gone channel aborts before any send
        ctx.services.validate_channel(event.channel_id)

        # rich DTO inspection (§ rich boundary): event.message is the frozen
        # adapters.model.DiscordMessage — None only for synthetic events.
        rich = event.message
        if rich is None or len(rich.embeds) < ctx.settings["min_embeds"]:
            return
        target = rich.button(ctx.settings["button_id"])   # searches the tree
        if target is None:
            ctx.logger.info(
                "no button %r on message %s (%s)",
                ctx.settings["button_id"], event.message_id, rich.summary(),
            )
            return

        # paced + audited capability actions through the task pipeline
        await ctx.services.add_reaction(
            event.channel_id, event.message_id, ctx.settings["ack_emoji"],
            priority=Priority.NORMAL,
        )
        await ctx.services.click_button(
            event.channel_id, event.message_id, ctx.settings["button_id"],
            priority=Priority.NORMAL,
        )
        ctx.logger.info(
            "claimed %r in %s",
            ctx.settings["button_id"],
            ctx.services.describe_channel(event.channel_id),   # never raises
        )


__all__ = ["LootclaimPlugin"]
```

### Why each piece is the way it is

| Piece | Source-backed reason |
|---|---|
| `isinstance(event, MessageReceived)` first | `PluginManager` also routes `MessageEdited`/`MessageDeleted`/`ReactionAdded` to `handle_event`; the base-class docstring demands a fast prologue (cost is multiplied by plugin count). |
| `event.content.strip() != ctx.settings["trigger"]` before any `await` | exactly the `roll`/`soulcopy` (test-fixture) trigger check, read from the **live** settings dict at event time. |
| `ctx.services.credentials.owner_user_ids` | `frozenset[int]` — the injected credentials view (`AccountPluginServices.credentials`; env `OWNER_USER_IDS` is the one-time admin-list seed); the plugin-side owner gate. |
| `ctx.services.validate_channel(...)` as the first flow step | raises `TargetGone` (a `FlowBlocked`) *before* anything is sent — the "no partial output" guarantee. |
| `rich.button(custom_id)` / `rich.summary()` | real helpers on `adapters.model.DiscordMessage` (frozen DTO, `repr=False` on `raw`). |
| `add_reaction`/`click_button` with explicit `Priority` | pipeline actions with defaults `NORMAL`; pacing/audit/cancellation come free. |
| `except FlowBlocked:` around the **whole** flow | one abort line, then return — mirrored from `tests/fixtures/sim_plugins/soulcopy/plugin.py`. |
| no-op `on_enable`/`on_disable`/`on_unload` | graceful no-op hooks; omitting them entirely is equally valid (base class defaults). |

### Things this plugin deliberately does NOT do

* It does **not** use the return value of `send_message`/`add_reaction`/`click_button` — they
  return `None` on success (fire-and-await-ack; see [PLUGIN_API.md §6/§11](PLUGIN_API.md)).
* It does **not** touch `event.raw` / `rich.unwrap()` — that couples code to `discord.py-self`
  and is runtime-only.
* It does **not** sleep between actions — pacing is the pipeline's job.

---

## 2. Minimal sketch: reading reactions

`ReactionAdded` arrives on `handle_event` with **no message fetch** — everything a
kakerareact-style plugin needs is on the event itself.

```python
"""reactlog: minimal ReactionAdded consumer (sketch)."""
from __future__ import annotations

from core.event_bus import Event, ReactionAdded
from core.plugin_base import PluginBase, PluginContext
from core.settings_manager import Setting


class ReactlogPlugin(PluginBase):
    name = "reactlog"
    version = "0.1.0"
    description = "Sketch: watch one emoji and log who reacted."

    settings = (
        Setting("watch_emoji", "str", "\N{WHITE HEAVY CHECK MARK}",
                "Emoji to watch for."),
    )

    async def on_load(self, ctx: PluginContext) -> None:
        ctx.logger.info("reactlog loaded (watching %r)", ctx.settings["watch_emoji"])

    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        if not isinstance(event, ReactionAdded):        # filter fast
            return
        if event.emoji != ctx.settings["watch_emoji"]:  # printable repr, e.g. "✅" or "<:name:id>"
            return
        if event.user_id not in ctx.services.credentials.owner_user_ids:
            return                                      # owner-gate every trigger
        # stable ids + printable emoji, no fetch; dm flag resolved from cache;
        # event.raw is the runtime-only library handle pair — do not persist.
        ctx.logger.info(
            "owner reacted in channel %s (message %s, dm=%s, guild=%s)",
            ctx.services.describe_channel(event.channel_id),
            event.message_id, event.dm, event.guild_id,
        )
```

Extension points from here, all with the public API only:

* **Answer a reaction** — `await ctx.services.add_reaction(event.channel_id, event.message_id,
  emoji, priority=Priority.NORMAL)`.
* **Inspect the reacted message when you already have it** — reactions arriving for a message you
  saw as `MessageReceived` can be matched by `event.message_id` against the rich DTO you kept
  (ids are the join key; never re-fetch).
* **Wait for a specific reactor** — `await ctx.services.wait_for_message(check, timeout)` is the
  message-side equivalent with the same ownership rules.

---

## 3. Copy-and-adapt pointers

* Simplest complete reference plugin: `plugins/roll/plugin.py` (55 lines: metadata, 3 settings,
  owner trigger, one send — the file's own docstring says "Copy this file as the template").
* Full multi-step flow (DM collection + chunking + the FlowBlocked abort contract):
  [plugins/soulcopy.md](plugins/soulcopy.md); line-by-line template notes in
  [plugins/roll.md](plugins/roll.md).
* How plugins are discovered, gated and driven: [PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md).
