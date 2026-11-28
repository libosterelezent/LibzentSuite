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
``add_reaction`` / ``click_button`` (all pipeline-paced, fire-and-await-ack;
``fetch_user`` returns ``None`` when the account's client cannot resolve
the user), ``wait_for_message`` / ``collect_messages`` (bus reads;
``wait_for_message`` returns ``None`` on timeout), ``expect`` (a
send-coupled reactive window — see the Expect windows section below),
``publish``, ``validate_channel`` / ``describe_channel`` (cache), the
persistent
account+plugin-scoped families ``cooldown_set`` / ``cooldown_remaining`` /
``cooldown_active`` / ``cooldown_clear`` / ``cooldown_list`` and
``state_get`` / ``state_set`` / ``state_delete``, plus the
``credentials`` (owner ids), ``account``, ``plugin`` and
``settings`` properties.

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

Expect windows
--------------
``expect(check, timeout)`` opens a send-coupled reactive window:

.. code-block:: python

    async with ctx.services.expect(
        check=lambda m: m.author_id == MUDAE, timeout=30
    ) as window:
        await ctx.services.send_message(channel_id, "$wa")  # INSIDE the window
        reply = await window.match()  # first match wins

Entering the window subscribes BEFORE the send runs, so a reply
landing during the send's queue wait matches exactly like one landing
after — the race a ``wait_for_message`` issued after a send loses is
closed by construction, and the framework owns and unwires the waiter
(no dangling plugin task). ``timeout`` is a total budget from entry
shared by every await; the window keeps observing the account's events
until the block exits.

Awaits: ``window.match()`` (first unconsumed match, ``None`` when the
deadline lapses), ``window.collect(n)`` (up to ``n`` matches, partial
on deadline — judge by ``len()``), ``window.first_of(check_a,
check_b)`` (first match of ANY check; the window's own check is not
consulted). Checks see the frozen event DTO; a raising check surfaces
as :class:`core.errors.AccountError` with the cause. Awaits after the
block exits raise ``RuntimeError`` — a stale window fails loud.

Priority & pacing
-----------------
:class:`core.priority.Priority` (``HIGH``/``NORMAL``/``LOW``) orders
grants inside the limiter. Every services action is paced (per-task +
group scopes, the learned per-action default intervals, jitter) —
plugins never self-throttle.

Self-commands & UI extension
----------------------------
Plugins may declare ``COMMANDS`` (a tuple of
:class:`core.plugin_base.CommandSpec`, whose ``args`` are declarative
:class:`core.plugin_base.ArgSpec` positional grammar) — each verb
becomes a ``<prefix><command>`` on every account the plugin is
assigned+enabled on; handlers receive a
:class:`core.plugin_base.CommandContext` (same ``services`` facade,
validated ``settings_api``, ``invoker_id``) and reply through the paced
pipeline. The optional ``EMITS`` / ``FOLLOWS`` declarations opt the
plugin into workspace chains (durable signals, follow handlers — see
:doc:`writing_plugins`).

Deep dives, examples, and the anti-pattern catalog live in the
LOCAL-ONLY ``knowledgebase/`` archive (on the machines that built it;
not present in a fresh clone).

