core — framework
================

Control facade
--------------
.. autoclass:: core.api.ControlAPI
   :members:

Composition
-----------
.. automodule:: core.bootstrap
   :members:

Event bus
---------

.. automodule:: core.event_bus
   :members:

Accounts
--------
.. automodule:: core.account_manager.manager
   :members: Account, AccountRuntime, AccountStatus, AccountManager

Rate limiting
-------------
.. automodule:: core.rate_limit_manager.engine
   :members: RateLimitManager, Grant, DEFAULT_ACTION_INTERVALS
.. automodule:: core.rate_limit_manager.policies
   :members: RatePolicy, parse_scope, MIN_ACTION_INTERVAL

Task scheduler
--------------
.. automodule:: core.task_scheduler.scheduler
   :members: TaskScheduler
.. automodule:: core.task_scheduler.tasks
   :members: Task

Settings & config
-----------------
.. automodule:: core.settings_manager
   :members: Setting, SettingsManager, ACCOUNT_SETTINGS
.. automodule:: core.config
   :members: load_config, ProjectConfig

Access management (admin panel store)
--------------------------------------
.. automodule:: core.access_manager
   :members: AccessManager, PREFIX_MODES, DEFAULT_ACCOUNT_ROW

Credentials views
-----------------
.. automodule:: core.credentials
   :members: Credentials

``AccountPluginServices.credentials`` returns the credentials view the
account manager was constructed with (the injected ``Credentials``); the
former ``core.framework_credentials.FrameworkCredentials`` wrapper was
never constructed anywhere and has been removed.

direct account→plugin assignment (``account_plugins`` table; see
``ControlAPI.accounts_plugin_assign`` / ``accounts_plugins_set``).
Per-plugin **setting** profiles ARE implemented:
``ControlAPI.plugin_profiles_*`` over the ``plugin_profiles`` table
(migration v7), with the channel-scope convention owned by
``core.plugin_scopes``.

Plugins
-------
.. automodule:: core.plugin_manager.manager
   :members: PluginManager
.. automodule:: core.plugin_base
   :members: PluginBase, PluginContext, PluginServices
.. automodule:: core.plugin_scopes
   :members: validate, channel_allowed, CHANNELS_KEY

Remote control
--------------
.. automodule:: core.remote_control
   :members: RemoteControl

Logging
-------
.. automodule:: core.logging_service
   :members: LogService, ServiceLogger, StdlibBridge,
             attach_stdlib_bridge, detach_stdlib_bridge, configure_stderr

Audit
-----
.. automodule:: core.audit
   :members: AuditRecorder, format_audit

Errors
------
.. automodule:: core.errors
   :members:
