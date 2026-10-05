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
      - ``MUDAE_BOT_ID``, ``OWNER_USER_IDS``, ``DASHBOARD_TOKEN``,
        ``REMOTE_CONTROL``, ``SELF_COMMAND_PREFIX`` (optional
        self-command prefix — see the owner-id requirement note in
        ``.env.example``) (+ ``SM_MASTER_KEY`` via process env only) —
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
      - per account; editable at runtime via Python/ControlAPI only
        (``account_setting_get``/``account_setting_set``) — there is no
        CLI command or dashboard route for them yet. The reconciliation
        cadence for out-of-band edits is env-tunable:
        ``SM_RECONCILE_INTERVAL_S`` (seconds; 0 = every status call;
        default 30).
   * - Plugin settings (DB)
      - per ``Setting`` schema, resolved ``defaults ← global ← account``
      - live view for running plugins

Rate scopes: per-task ``(account, action)``, per-account/group
 ``(group:<sync_group>, action)`` shared clock, server-wide lane — the
strictest wait wins. Jitter is drawn before admission and slept after
the grant, with spacing clocks stamped at the expected wire instant, so
wire gaps keep the effective interval.
