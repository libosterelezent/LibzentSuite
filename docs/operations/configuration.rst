Configuration
=============

Complete settings reference: ``knowledgebase/CONFIGURATION.md`` (if you
maintain a local knowledgebase — not shipped in a fresh clone). Summary
of the layers:

.. list-table::
   :header-rows: 1

   * - Layer
     - Where
     - Applies to
   * - ``.env``
     - ``PRIMARY_BOT_ID``, ``OWNER_USER_IDS``
       (+ ``LZ_MASTER_KEY`` via process env only) —
       account tokens live ENCRYPTED in the DB, imported from ``.env``
       only via ``credentials import-env``
     - secrets & ids (never in the DB)
   * - ``config/rate_limits.json``
     - per-action ``{interval, jitter:[min,max]}`` at ``global``/``accounts.<name>`` + ``lane {enabled, prefixrate}``
     - rate policies (floor 1.5s enforced; runtime lane state persisted in kv)
   * - ``config/global_settings.json``
     - plugin setting overrides (layer 2 of 3)
     - all accounts
   * - Account settings (DB)
     - ``reconnect_*``, ``typing_probability``
     - per account, editable at runtime
   * - Plugin settings (DB)
     - per ``Setting`` schema, resolved ``defaults ← global ← account``
     - live view for running plugins

Rate scopes: per-task ``(account, action)``, per-account/group
 ``(workspace:<name>, action)`` shared clock — the
strictest wait wins. Jitter is drawn before admission and slept after
the grant, with spacing clocks stamped at the expected wire instant, so
wire gaps keep the effective interval.

Environment variables
---------------------

Process environment (never the ``.env`` file):

.. list-table::
   :header-rows: 1

   * - Variable
     - Values
     - Effect
   * - ``LZ_MASTER_KEY``
     - a Fernet key
     - Master key for credentials at rest; overrides the key file
       (``state/credentials.key``). A malformed value fails closed.
   * - ``LIBZENT_CONSOLE_LEVEL``
     - ``DEBUG`` / ``INFO`` / ``WARNING``
     - Console log level; anything else means ``WARNING``. The REPL stays
       readable by default — everything still persists to the ring and DB.
   * - ``LZ_DB_TELEMETRY``
     - ``1`` / ``true``
     - DB worker telemetry. Off means zero overhead by contract.
       ``--debug`` enables it.
   * - ``LZ_SOAK``, ``LZ_CHAOS``
     - any non-empty value (``1``)
     - Opt in to the long soak and chaos test suites.

On Windows set them per shell with ``$env:NAME = "value"`` (PowerShell) or
persist one with ``setx NAME value``.

The credential key file's ``0600`` mode is a POSIX mechanism: Windows
neither honours the bits nor runs the boot permission audit, so protect
``state/`` with file-system ACLs there.
