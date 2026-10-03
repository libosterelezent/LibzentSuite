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
  — CLI and dashboard are 1:1 wrappers (parity by construction).
* Plugins never touch clients, SQLite, or the limiter directly; everything
  goes through the ``PluginServices`` protocol.
* Token *plaintext* never enters the database (Fernet ciphertext only; the key lives outside the DB in `state/credentials.key` / `SM_MASTER_KEY`); ``.env`` stores them, rows store
  references.

Details: ``knowledgebase/COMPONENT_MAP.md``.
