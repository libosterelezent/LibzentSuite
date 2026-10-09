database — persistence
======================

.. automodule:: database.db
   :members: Database
.. automodule:: database.models
   :members: AccountRecord, ProfileRecord, PluginMetadata, RatePolicyRecord
.. automodule:: database.repositories.accounts
   :members: AccountsRepository
.. automodule:: database.repositories.account_plugins
   :members: AccountPluginsRepository
.. automodule:: database.repositories.cache
   :members: CacheRepository
.. automodule:: database.repositories.logs
   :members: AuditRepository, LogRepository
.. automodule:: database.repositories.tasks
   :members: TaskHistoryRepository

Schema/migrations: ``knowledgebase/DATABASE.md`` (tables, indexes,
pruning, cascades, secrets policy) — if you maintain a local
knowledgebase; it is not shipped in a fresh clone.
