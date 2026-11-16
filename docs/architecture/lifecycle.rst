Runtime lifecycle
=================

Boot
----
``main.py`` → :func:`core.bootstrap.build` (order pinned by tests):
credentials → config → Database+migrations → LogService(+bridge) → bus →
limiter → settings → audit → scheduler → plugins.discover →
accounts (DB restore) → sender bind → ``scheduler.start()`` → remote
control → ControlAPI → surface.

Account
-------
``add_account`` persists FIRST (repo failure leaves memory untouched);
``start`` resolves the token (encrypted DB credential store; `.env` keys
are migration-only), wires every
client callback (message + rich events) before connect, connects, ingests
the READY snapshot (zero API calls), activates the account's assigned
plugins (direct ``account_plugins`` rows),
spawns the reconnect supervisor (exponential backoff from account
settings).

Shutdown
--------
``Framework.shutdown``: remote control unsubscribes → scheduler drains →
accounts stop (plugins deactivate first) → **logging sinks detach before
``database.close()``** (the bridge must never feed a closed sink) → DB
closes. The global emergency stop halts everything; ``system_resume``
(``estop`` resume / ``POST /api/resume``) re-arms the scheduler — accounts
restart explicitly.

Exact walk-through: ``knowledgebase/RUNTIME_LIFECYCLE.md`` (if you
maintain a local knowledgebase — not shipped in a fresh clone).
