Boundaries and ownership
========================

=========================  =====================  =====================================
Component                  Owns                   May call
=========================  =====================  =====================================
account_manager            Account runtime/cache  plugin_manager, scheduler, limiter
plugin_manager             Plugin runtimes        settings, bus
rate_limit_manager         Policies, pacing       (none — leaf service)
task_scheduler             Task lifecycle         limiter, sender hook
event_bus                  Subscriptions          (none — leaf)
logging_service            Canonical log entries  stdlib mirror (guarded)
database                   SQLite + repositories  (none — leaf)
adapters                   Discord boundary       core.errors only
interfaces (cli/dash)      Presentation           ControlAPI ONLY
remote_control             Mention bridge         ControlAPI, scheduler
=========================  =====================  =====================================

Hard rules enforced by review and tests:

* ``core`` never imports adapters at runtime (event DTOs are typed
  ``Any`` on the core side).
* Interfaces mutate state exclusively through :class:`core.api.ControlAPI`
  — CLI and dashboard both wrap it (near-parity by construction; a few
  dashboard surfaces — admin-panel management, profile editing, channel
  lists, effective-settings view, bulk plugin-set — have no CLI verb).
* Plugins never touch clients, SQLite, or the limiter directly; everything
  goes through the ``PluginServices`` protocol.
* Account tokens are stored as Fernet **ciphertext in the database** —
  plaintext never enters it, and the key lives outside the DB
  (``state/credentials.key`` 0600 or ``SM_MASTER_KEY`` in the process
  environment). ``.env`` holds ids/auth switches only; legacy
  ``DISCORD_TOKEN_*`` entries are read just once by the
  ``credentials import-env`` migration into the encrypted store.

Details: ``knowledgebase/COMPONENT_MAP.md`` (committed with the
repository).
