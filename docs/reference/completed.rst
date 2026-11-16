Completed Features
=================

This page records what has been built and verified, with pointers to
the knowledgebase for full detail. It is the historical record
AGENTS.md rule 8 requires — completed roadmap items migrate here.

Workspace Unification (2026-11-13 → 2026-11-14)
-----------------------------------------------

ONE concept (the **workspace**) replaced sync groups + the fleet
module. Workspaces own membership (the ``accounts.workspace``
pointer), pacing clocks (shared serialization), and chains
("account A acts → follow-up account B responds, visibly"). The old
modules and their five tables were deleted; migration v10 folded the
data, v11 dropped the old shapes.

**Key files:** ``core/workspace/`` (service, models, transitions,
policy, chains runtime, durable signals, guard, reconciler)

Command Convergence (2026-11-14 → 2026-11-15)
---------------------------------------------

Plugins declare their command face as a real library **Cog**
(``COG = MyCog`` on PluginBase) — typed converter parameters,
command groups, native help, cog hooks. The manual COMMANDS facade
(CommandSpec/ArgSpec/tokenization/registration bridge) retired. All
15 framework verbs ride the FrameworkVerbsCog.

**Key files:** ``core/plugin_manager/cog_host.py``,
``core/plugin_manager/framework_cog.py`` (the C1 intake mixin retired
to ``dump/`` 2026-11-15 — the adapter wires its own event handlers)

better.py-self Fork (fully adopted)
-----------------------------------

The user-account Discord client fork with memory-capped caches,
per-account cache persistence, dynamic callable prefixes, the
authorization gate via ``bot.add_check()`` (evaluated before argument
parsing), and the durable signal outbox.

**Key files:** ``adapters/discord/client.py``,
``core/workspace/signals.py``

FastAPI Core API (2026-11-14)
-----------------------------

REST + WebSocket surface over the one ControlAPI facade: Pydantic
models, token auth, the full workspace/account/plugin/task/log/audit
surface. Zero business logic in the interface layer.

**Key files:** ``interfaces/api/app.py``

Flet UI Shell (2026-11-14)
--------------------------

The dashboard-replacement shell: page registry, API client, flet
NavigationRail with Status/Accounts/Workspaces/Logs pages. Flet
imports lazily (testable without the UI dependency).

**Key files:** ``interfaces/ui/__init__.py``
