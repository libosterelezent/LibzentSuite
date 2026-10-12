Writing a plugin
================

The framework ships with **no bundled plugins** — drop a package under
``plugins/`` (or pass ``--plugins <dir>``) and it is discovered
automatically at boot. A plugin is a self-describing
:class:`core.plugin_base.PluginBase` subclass: metadata, a settings
manifest, lifecycle hooks, event handling, and optionally declared
Discord **self-commands** and **dashboard UI extension** hooks.

The complete contract reference is ``knowledgebase/PLUGIN_SYSTEM.md``
(with ``knowledgebase/PLUGIN_API.md`` as the formal API reference);
this page is the practical guide. The minimal working example lives at
``tests/fixtures/plugins/probe/`` (the test fixture the suite
exercises — never discovered by a production boot).

Anatomy
-------

.. code-block:: python

   from core.event_bus import Event, MessageReceived
   from core.plugin_base import (
       ArgSpec, CommandContext, CommandSpec, PluginBase, PluginContext,
   )
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
auto-generates the CLI ``set`` validation and the dashboard editors —
parity by construction.

Resolution layers (highest wins):

1. schema defaults,
2. the global layer (``config/global_settings.json``),
3. **the bound plugin profile** (a named settings bundle created in the
   dashboard Plugins/Profiles tab; may carry a channel
   allow/deny scope under the reserved ``channels`` key —
   ``core.plugin_scopes`` owns that structure),
4. per-account overrides (``plugin_settings`` rows — the dashboard/CLI
   ``set-setting`` surface; ``value: null`` deletes the override).

``ctx.settings`` is the LIVE resolved dict — changes made at runtime are
seen without a rebuild.

Self-commands: the declarative COMMANDS grammar
-----------------------------------------------

Declare a ``COMMANDS`` tuple on the class and each verb becomes a
Discord command (``<prefix><name>``) on every account the plugin is
ASSIGNED to and ENABLED on. Handlers may be plain methods (bound to the
per-account instance):

.. code-block:: python

       # handlers first (a class body is sequential), COMMANDS last:
       async def _cmd_myset(self, ctx: CommandContext, args) -> str:
           p = ctx.parsed            # typed + validated BEFORE the handler
           return f"set: name={p['name']} mode={p['mode']} count={p['count']}"

       async def _cmd_mystart(self, ctx: CommandContext, args) -> str:
           names = ctx.parsed.get("names") or []
           return "started: " + ", ".join(names) if names else "nothing"

       COMMANDS = (
           CommandSpec(
               name="myset",
               help="set a named profile up",
               handler=_cmd_myset,      # bare method ref -> bound per account
               args=(
                   ArgSpec("name"),                          # required str
                   ArgSpec("mode", choices=("fast", "slow", "auto")),
                   ArgSpec("count", kind=int),               # typed
                   ArgSpec("note", required=False, default=""),  # optional
               ),
           ),
           CommandSpec(
               name="mystart",
               help="start one or more things by name",
               handler=_cmd_mystart,
               args=(ArgSpec("names", greedy=True),),   # <names...> list
           ),
       )

Rules of the grammar:

* ``name`` must match ``[a-z0-9_]+`` and must not collide with a
  reserved framework verb (``pg``, ``say``, ``help``, ``prefixrate``,
  ``adminlist``, ``adminperms``, ``adminprefix``) — collisions are
  dropped with a warning (first registration wins among plugins too).
* The handler returns a string; it is sent back through the paced
  pipeline in the invoking channel (1900-char cap). Raising
  ``ValueError`` renders the auto-generated usage line to the invoker.
* ``ArgSpec(kind=...)`` accepts ``str``/``int``/``float`` or any
  ``callable(text) -> value``; ``choices`` are checked after
  conversion; ``greedy`` consumes the remaining tokens as a list.
  Parse failures never reach the handler — the invoker sees
  ``usage: <name> <auto usage line>`` plus the reason.
* ``ctx`` is a :class:`core.plugin_base.CommandContext`: the SAME
  ``services`` facade plugin code uses (no raw client), the invoking
  ``channel_id``/``guild_id``/``invoker_id``, and ``settings_api`` — a
  validated surface (``schema``/``effective``/``set``/``clear``/
  ``binding``/``profiles``) matching the dashboard set-setting path.
* Who may invoke is decided by the admin panel
  (``AccessManager.can_invoke``: per-account allowlists + admin bypass,
  fail-closed) — never by the plugin.
* The framework ``help`` verb lists your commands under the plugin's
  name automatically (descriptions truncated at 25 chars, paginated).

UI extension: stat cards and sidebar panels
-------------------------------------------

Two optional hooks extend the dashboard with STRUCTURED data (never
HTML — the dashboard's own templates escape every value, so plugin
output is XSS-safe by construction):

.. code-block:: python

       from core.plugin_base import PanelSection, StatCard, UIPanel

       async def ui_stats(self, ctx: PluginContext):
           return (
               StatCard(label="items", value="42", sub="tracked",
                        level="ok"),
           )

       async def ui_panels(self, ctx: PluginContext):
           return (
               UIPanel(
                   id="myplugin:overview",       # unique "plugin:panel"
                   title="My Plugin",
                   sections=(
                       PanelSection(kind="metrics", title="Live", data=[
                           {"label": "account", "value": ctx.account},
                       ]),
                       PanelSection(kind="table", title="Recent", data={
                           "columns": ["when", "what"],
                           "rows": [["12:00", "thing"]],
                       }),
                       PanelSection(kind="list", title="Settings", data=[
                           {"label": "factor",
                            "value": str(ctx.settings.get("factor"))},
                       ]),
                       PanelSection(kind="text", title="About", data=(
                           "Plain paragraph, escaped at render time."
                       )),
                   ),
               ),
           )

``ui_stats`` feeds the Home tab of the selected account;
``ui_panels`` contributes framework-level sidebar entries (first
instance per plugin wins, deduped by id). A crashing provider
contributes nothing — isolation is the framework's job.

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
(``account_plugins`` rows — applied immediately, no restart); bind a
settings profile per account in the dashboard (Plugins/Profiles tab)
to ship a reusable settings bundle with a channel scope. Test offline
with the simulator (``python3 main.py --sim-list``).
