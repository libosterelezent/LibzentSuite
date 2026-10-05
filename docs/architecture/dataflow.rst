Data flow
=========

Inbound message
---------------

.. code-block:: text

   discord.py-self / SimWorld event
      → adapters.model.convert_message()      (ONE non-blocking conversion)
      → DiscordMessage (frozen rich DTO + raw escape hatch)
      → MessageReceived(flat fields + .message) → EventBus
      → plugin dispatch (per-account, error-isolated)
      → audit 'in' row (content preview + embed/component counts)

Outbound action (message / dm / reaction / button)
--------------------------------------------------

.. code-block:: text

   PluginServices.send_message() → scheduler.submit()      [TaskQueued]
      → per-account worker lane (concurrency = 1)
      → RateLimitManager.acquire(): lane → server cooldowns →
        pacing (per-task (account,action) + group shared clock) → jitter
      → AccountManager.send_hook() (cache validation; DM resolved
        memory → api_cache → ONE client call)
      → adapter → audit + history + TaskCompleted (exactly-once)

Management
----------

CLI ``run_command`` / dashboard route / Discord ``@account <cmd>`` all
resolve to the same :class:`core.api.ControlAPI` methods; persistence and
validation happen once, below the facade.

Deep version: ``knowledgebase/DATA_FLOW.md`` (if you maintain a local
knowledgebase — not shipped in a fresh clone).
