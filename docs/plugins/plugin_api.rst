Plugin API reference
====================

The supported **public plugin API** (stability tier: PUBLIC). Everything
else in ``core`` is internal; underscore names are private. Object
reference entries live in :doc:`/api/adapters` and :doc:`/api/core`;
this page defines the *contract*.

Contract
--------
:class:`core.plugin_base.PluginBase` — subclass contract: metadata
(``name``/``version``/``description``), a ``settings`` manifest of
:class:`core.settings_manager.Setting`, lifecycle hooks (``on_load``,
``on_enable``, ``on_disable``, ``on_unload``) and ``handle_event``.

:class:`core.plugin_base.PluginContext` — what a plugin may touch:
``account``, ``plugin``, ``settings`` (live view), ``services``, ``logger``.

:class:`core.plugin_base.PluginServices` — the ONLY Discord surface:
``send_message`` / ``send_dm`` / ``open_dm`` / ``fetch_user`` /
``add_reaction`` / ``click_button`` (all pipeline-paced, fire-and-await-ack),
``wait_for_message`` / ``collect_messages`` (bus reads),
``publish``, ``validate_channel`` / ``describe_channel`` (cache),
``credentials`` (owner ids, Mudae id).

Events plugins receive
----------------------
``handle_event`` sees the account-scoped event types:
:class:`core.event_bus.MessageReceived` (flat fields + ``.message`` rich
DTO), :class:`core.event_bus.ReactionAdded`,
:class:`core.event_bus.MessageEdited`,
:class:`core.event_bus.MessageDeleted`. Filter fast for unrelated types.

Rich message DTO
----------------
``event.message`` is a frozen :class:`adapters.model.DiscordMessage`:
embeds/components/attachments/reactions/reference/author/channel/guild,
``buttons``/``button(custom_id)`` helpers, ``summary()`` for logging, and
the runtime-only ``unwrap()`` escape hatch. Missing optionals are ``None``
by design — never fetched to populate.

Priority & pacing
-----------------
:class:`core.priority.Priority` (``HIGH``/``NORMAL``/``LOW``) orders
grants inside the limiter. Every services action is paced (per-task +
group scopes, server-wide lane, 1.5s floor, jitter) — plugins never
self-throttle.

Deep dive (why/ownership/errors per method): ``knowledgebase/PLUGIN_API.md``.
Examples: ``knowledgebase/PLUGIN_EXAMPLES.md``. Anti-patterns:
``knowledgebase/PLUGIN_ANTIPATTERNS.md``.
