"""Component map — SlaughterMudae's managers (user's architecture, §1).

Automation Framework
├── Account Manager      -> core.container.AccountContainer (+ core.account)
├── Plugin Manager       -> core.plugin_loader + core.plugin_base + core.container plugins
├── Profile Manager      -> core.profiles.ProfileManager
├── Task Scheduler       -> core.scheduler.TaskScheduler
├── Rate Limit Manager   -> core.rate_limit_manager (token buckets + REST buckets
│                          + delays + SendEngine serialization)
├── Event Bus            -> core.events.EventBus
├── Settings Manager     -> core.settings.SettingsStore (+ config loader)
├── Database             -> core.database.Database
└── Logging              -> core.logging_setup (+ RingBufferHandler tail)

Live mode:   Automation Framework -> Discord Adapter (core.discord_adapter)
             -> user-account client library
Testing:     Automation Framework -> Simulation Adapter (core.simulator)
"""
