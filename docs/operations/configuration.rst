Configuration
=============

Complete settings reference: ``knowledgebase/CONFIGURATION.md``. Summary
of the layers:

.. list-table::
   :header-rows: 1

   * - Layer
     - Where
     - Applies to
   * - ``.env``
     - ``MUDAE_BOT_ID``, ``OWNER_USER_IDS``, ``DISCORD_TOKEN_<NAME>``, ``DASHBOARD_TOKEN``, ``REMOTE_CONTROL``
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
``(group:<sync_group>, action)`` shared clock, server-wide lane — the
strictest wait wins, then jitter.
