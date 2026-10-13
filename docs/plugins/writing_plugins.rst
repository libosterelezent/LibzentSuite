Writing a plugin
================

The framework ships with **no bundled plugins** — drop a package under
``plugins/`` (or pass ``--plugins <dir>``) and it is discovered
automatically at boot. A plugin is a self-describing
:class:`core.plugin_base.PluginBase` subclass: metadata, a settings
manifest, lifecycle hooks, event handling, and optionally declared
Discord **self-commands**, **dashboard UI extension** hooks and
**builder-based dashboard pages**.

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

Dashboard UI: builder-based plugin pages
----------------------------------------

For full pages (tabs, forms, tables, actions) declare ONE optional
SYNC hook and build a component tree with the
:class:`core.dashboard_ui.DashboardUIToolset` builders. The complete
author's guide is ``knowledgebase/DASHBOARD_UI_API.md``; the working
reference example is ``tests/fixtures/plugins/config_demo/plugin.py``
(exercised end-to-end by
``tests/unit/test_dashboard_ui_acceptance.py``).

.. code-block:: python

   from core.dashboard_ui import DashboardUIToolset, ActionResult

   def initialize_dashboard_ui(self, ui: DashboardUIToolset) -> None:
       page = ui.Page().set_id("myplugin").set_title("My Plugin")
       section = ui.Section().set_title("Controls")
       card = ui.Card().set_title("Actions")

       card.add_component(
           ui.Button(component_id="run")
           .set_label("Run")
           .set_variant("primary")
           .set_action(self._on_run))
       card.add_component(
           ui.Metric().set_label("Status").set_value("idle"))

       section.add_component(card)
       page.add_section(section)
       self.attach_dashboard_ui(ui.Layout().add_page(page))

   async def _on_run(self, context, payload) -> ActionResult:
       return ActionResult(success=True, message="done",
                           refresh=["events-table"])

The essentials:

* **Builders** — fluent setters returning ``self`` (``Layout`` →
  ``Page`` → ``Tab``/``Section`` → ``Card``/``Row``/``Column`` →
  ``Text``/``Badge``/``Metric``/``Progress``, the eight input types,
  ``ListView``/``Table``, ``Button``, ``Form``). Every argument is
  validated immediately; failed mutations never corrupt the tree.
* **IDs** — automatic ``uuid4().hex[:12]`` per component, or explicit
  ``[A-Za-z0-9_-]{1,64}`` (use explicit ids for anything you target:
  ``refresh`` lists, lookups). Duplicates raise
  :class:`core.dashboard_ui.DuplicateComponentIdError` at add time and
  :class:`core.dashboard_ui.TreeValidationError` at attachment.
* **Actions** — ``set_action(callable)`` IS the registration (direct
  Python callables, no manual signup). Signature
  ``(ActionContext, payload) → ActionResult``; sync or async;
  ``payload`` is ``{}`` for buttons, ``{"value": …}`` for standalone
  inputs, ``{field_id: value}`` for form submits. ``context.services``
  is the same paced facade lifecycle hooks use; ``context.account`` is
  the dashboard's selected account. Callback failures are contained
  (logged, surfaced as a plain ``UIError``).
* **Forms** — ``Form.add_field(...)`` (the eight input types only) +
  ``set_submit_action``; failed validation returns
  ``ActionResult(success=False, field_errors={field_id: message})``
  and the form renders each error inline under its input.
* **Data providers** — ``Table``/``ListView`` +
  ``set_data_source(callable)`` return JSON-safe rows (lists capped at
  500, nesting ≤ 8); fetches are lazy and visibility-checked.
* **Visibility** — ``set_check(callable)`` on ANY component,
  fail-closed (falsey return OR raised error hides it); ancestor
  chains compose; applied to the rendered tree AND every server-side
  request beneath it. Checks express state conditions, not viewer
  identity (the dashboard has one shared token).
* **Disabled** — ``set_disabled(bool)`` is UI state, NOT
  authorization: the server still dispatches a disabled component's
  action.
* **Lifecycle & scope** — attach validates + freezes the tree
  (mutation then raises :class:`core.dashboard_ui.FrozenLayoutError`);
  updates are explicit (build a fresh tree, re-attach); reload/unload
  detaches stale registrations; UI-init failures never fail the
  account activation. ONE UI per plugin (the first account to
  activate wins) — per-account behavior belongs in callbacks/providers.

Served by ``GET /api/dashboard-ui/navigation``, ``GET
/api/dashboard-ui/page``, ``POST /api/dashboard-ui/action`` and ``GET
/api/dashboard-ui/data``; rendered by the dashboard's generic
component renderer (``static/js/dashboard-ui.js``) — your tree is
XSS-safe by construction (structured data only, escaped at render).

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


Dashboard UI runtime guarantees
    The builder-based Dashboard UI API enforces its contract at runtime:
    bound-method actions re-bind to the live plugin instance (rejected
    when no account runtime is active), ``require_account=True`` actions
    receive a validated account + services context, ``set_disabled`` and
    visibility checks are enforced server-side (forged requests cannot
    bypass them), form submissions are schema-validated before the
    submit callback runs, and synchronous callbacks run in a bounded
    worker lane in the served stack. Full contract:
    ``knowledgebase/DASHBOARD_UI_API.md`` §15.
