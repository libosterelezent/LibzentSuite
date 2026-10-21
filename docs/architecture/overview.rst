System overview
===============

The controlling principle: **the framework controls the flow; plugins
control API usage; the RateLimiter controls when calls may execute.**

.. code-block:: text

                     ┌─────────────────────┐
                     │   LibzentSuite    │
   Events ──────────►│ Event routing       │
                     │ Account control     │
                     │ Task orchestration  │
                     └──────────┬──────────┘
                                │  (events/context only)
                                ▼
                     ┌─────────────────────┐
                     │       Plugins       │
                     │ decide WHEN an API  │
                     │ call is required    │
                     └──────────┬──────────┘
                                │ on demand (PluginServices)
                                ▼
                     ┌─────────────────────┐
                     │     RateLimiter     │
                     │ · per-task          │
                     │ · per-account/group │
                     │ · server-wide lane  │
                     └──────────┬──────────┘
                                ▼
                          API functions

Composition order (``core/bootstrap.py``): credentials → config →
Database (async `connect()` opens the connection and runs migrations) → LogService (+ DB sink +
stdlib bridge) → EventBus → RateLimitManager (config policies, lane from
kv) → SettingsManager → AuditRecorder → TaskScheduler (late-bound sender)
→ PluginManager.discover → ProfileManager → AccountManager (restores
persisted accounts) → sender bound → scheduler start →
ControlAPI (self-command hub bridged via ``COMMAND_HUB.api_provider``) →
surface (REPL / one-shot CLI / dashboard / headless).

Full digest: ``knowledgebase/ARCHITECTURE.md`` and
``knowledgebase/RUNTIME_LIFECYCLE.md`` — if you maintain a local
knowledgebase; it is not shipped in a fresh clone.
