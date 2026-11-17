Writing a plugin
================

The framework ships with **no bundled plugins** — drop a package under
``plugins/`` (or pass ``--plugins <dir>``) and it is discovered
automatically at boot. A plugin is a self-describing
:class:`core.plugin_base.PluginBase` subclass: metadata, a settings
manifest, lifecycle hooks, event handling, and optionally declared
Discord **self-commands** and **workspace chain declarations**
(``EMITS`` / ``FOLLOWS``).

The complete contract reference is ``knowledgebase/PLUGIN_SYSTEM.md``
(with ``knowledgebase/PLUGIN_API.md`` as the formal API reference);
this page is the practical guide. The minimal working example lives at
``tests/fixtures/plugins/probe/`` (the test fixture the suite
exercises — never discovered by a production boot).

Anatomy
-------

.. code-block:: python

   from core.event_bus import Event, MessageReceived
   from core.plugin_base import PluginBase, PluginContext
   from core.priority import Priority
   from core.settings_manager import Setting


   class MyPlugin(PluginBase):
       name = "myplugin"          # slug: [a-z0-9_-]+, unique
       version = "1.0.0"
       description = "What it does."
       settings = (
           Setting("trigger", "str", "go", "Owner command word."),
           Setting("factor", "float", 2.0, "Bounded knob.",
                   min_value=0.1, max_value=10.0),
       )

       def __init__(self) -> None:
           self._ctx = None        # the live context of the current activation

       # ── lifecycle ────────────────────────────────────────────────
       async def on_load(self, ctx: PluginContext) -> None:
           self._ctx = ctx
           ctx.logger.info("loaded (trigger=%r)", ctx.settings["trigger"])

       async def on_enable(self, ctx: PluginContext) -> None:
           self._ctx = ctx

       async def on_disable(self, ctx: PluginContext) -> None: ...
       async def on_unload(self, ctx: PluginContext) -> None:
           self._ctx = None

       # ── events ───────────────────────────────────────────────────
       async def handle_event(self, ctx: PluginContext, event: Event) -> None:
           if not isinstance(event, MessageReceived):
               return                                  # filter FAST
           if event.content.strip() != ctx.settings["trigger"]:
               return
           if event.author_id not in ctx.services.credentials.owner_user_ids:
               return                                  # access gate
           try:
               ctx.services.validate_channel(event.channel_id)  # TargetGone aborts
               await ctx.services.send_message(
                   event.channel_id, "done", priority=Priority.LOW)
           except Exception as exc:                    # FlowBlocked family
               ctx.logger.info("flow aborted: %s", exc)

Every send/react/click flows through the centralized scheduler and rate
limiter — pacing (learned defaults or your policies), jitter,
serialization, audit and cancellation apply automatically. Catch
:class:`core.errors.FlowBlocked` around multi-step flows and log ONE
clean abort line.

Settings: schema, layers, profiles
----------------------------------

``settings`` is a tuple of :class:`core.settings_manager.Setting`
(``name``, ``type`` in ``str|int|float|bool``, validated ``default``,
optional numeric bounds, description). The same manifest
auto-generates the CLI ``set`` validation —
parity by construction.

Resolution layers (highest wins):

1. schema defaults,
2. the global layer (``config/global_settings.json``),
3. **the bound plugin profile** (a named settings bundle created in the
   presets (applied by copy, with provenance); may carry a channel
   allow/deny scope under the reserved ``channels`` key —
   ``core.plugin_scopes`` owns that structure),
4. per-account overrides (``plugin_settings`` rows — the CLI
   ``set-setting`` surface; ``value: null`` deletes the override).

``ctx.settings`` is the LIVE resolved dict — changes made at runtime are
seen without a rebuild.

Self-commands: the plugin COG
-----------------------------

The command face is a real library Cog (the 2026-11-15 convergence):
declare ``COG = MyCog`` on the plugin class and the cog host builds one
instance per (account, plugin) — hosted exactly while the plugin is
ASSIGNED to that account and ENABLED on it (add_cog/remove_cog is the
gate). ``plugins/ping/plugin.py`` is the reference implementation.

.. code-block:: python

       from discord.ext import commands

       class MyCog(commands.Cog):
           def __init__(self, *, services=None) -> None:
               self.services = services   # the CogServices toolbox

           @commands.command(name="myset")
           async def myset(self, ctx, name: str, mode: str = "fast") -> None:
               await ctx.services.my_action(...)   # paced pipeline
               await ctx.send(f"set {name}")        # the command reply

       class MyPlugin(PluginBase):
           COG = MyCog
           ...

The discipline (enforced by the seams, not by convention):

* ``ctx.services`` is the per-(account, plugin) paced facade — the SAME
  one event handlers use, lifted onto the command context by the
  framework's ``before_invoke``. Sends through it are paced, queued,
  and audited. It is ABSENT when the stack is unwired (tests, stripped
  setups): read it with ``getattr(ctx, "services", None)`` and degrade
  with a clear reply — never a raw client send.
* ``ctx.send`` is for COMMAND RESPONSES ONLY — the one documented
  library-native send. Loops and proactive traffic ride
  ``ctx.services.send_message``.
* The framework verbs no longer ride accounts (owner call
  2026-11-17) — the per-account command surface is exactly the
  attached plugins' COGs, and the library's help lists precisely
  that. The admin gate still decides who may invoke (fail-closed) —
  never the plugin.
* The toolbox (``self.services`` in the cog) also carries
  ``to_rich``/``audit_rich`` for first-hand message handling in
  ``@commands.Cog.listener`` methods — the event-side utilities.

Chain declarations: EMITS and FOLLOWS
-----------------------------------

Plugins opt into workspace chains (the coordinated flows where account
A acts and a follow-up handler runs on account B) with two optional
ClassVar declarations:

.. code-block:: python

   from core.plugin_base import PluginBase
   from core.workspace import FollowSpec

   class MyPlugin(PluginBase):
       name = "myplugin"

       #: bare signal names this plugin may emit (namespacing
       #: "<plugin>:<name>" is added at consumption — one plugin cannot
       #: spoof another's signal)
       EMITS = ("claim_done",)

       #: triggers this plugin follows, each naming ONE handler method
       FOLLOWS = (
           FollowSpec(signal="grinder:claim_done",
                      handler="on_claim_done"),
           FollowSpec(task_action="message",
                      handler="on_message_done"),
       )

       async def on_claim_done(self, ctx, payload):
           # runs on the member the chain routed to; sends ride the
           # paced pipeline like any plugin code
           await ctx.services.send_message(payload["channel_id"],
                                           "cleaned up")

       async def on_message_done(self, ctx, payload):
           ...

Emission is durable — ``await ctx.services.emit_signal(name, payload)``
persists the signal BEFORE it acknowledges, delivery is at-least-once,
and consumers dedupe by the returned stable ``signal_id``. Workspaces
bind the declarations into named chains; a step whose plugin is not
assigned to the routed account is a *visible skip with the reason
recorded*, never a silent drop. The loader fails a broken declaration
at discovery (a missing handler, a namespaced EMIT name) — the same
fail-fast convention as the plugin COG.
Persistence helpers (no SQL)
----------------------------

Through ``ctx.services``: ``cooldown_set/remaining/active/clear/list``
(persistent, restart-correct cooldowns for this account+plugin) and
``state_get/set/delete`` (a small JSON key/value store scoped the same
way). Both raise :class:`core.errors.PluginError` on stacks that did
not wire the underlying service.

Running it
----------

.. code-block:: bash

   python3 main.py plugins list                       # your manifest
   python3 main.py accounts add alpha myplugin        # assign at creation
   python3 main.py accounts plugin-add alpha myplugin # or later (immediate)

A discovered plugin runs on an account only via a DIRECT assignment
(``account_plugins`` rows — applied immediately, no restart); presets
(previewed, then applied by copy with provenance) ship reusable
settings bundles. Test offline with the simulator
(``python3 main.py --sim-list``).

