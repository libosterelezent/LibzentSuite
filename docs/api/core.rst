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
   :members: RateLimitManager, Grant
.. automodule:: core.rate_limit_manager.policies
   :members: RatePolicy, parse_scope
.. automodule:: core.rate_limit_manager.lane
   :members: GlobalSendLane

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

Profiles
--------
.. automodule:: core.profile_manager.manager
   :members: ProfileManager

Plugins
-------
.. automodule:: core.plugin_manager.manager
   :members: PluginManager

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

Credentials
-----------
.. automodule:: core.credentials
   :members: Credentials
