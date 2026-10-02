"""Composition root: builds the ENTIRE framework from config + credentials.

Construction order (the only place that knows how the managers interconnect):

  Credentials -> Config -> Database(await connect) -> LogService(+db sink)
    -> EventBus -> RateLimitManager(+config policies, await load_persisted)
    -> SettingsManager(await load) -> AuditRecorder
    -> TaskScheduler(late-bound sender) -> PluginManager(discover + await
    persist_metadata) -> ProfileManager(await load)
    -> AccountManager(await load; restores persisted accounts)
    -> CredentialService(+refresh)/CooldownService(await restore)/
       PluginStateService wired into the managers
    -> sender bound (accounts.send_hook) -> scheduler.start()
    -> SimulationRunner -> ControlAPI

``build`` is ASYNC (``framework = await build(...)``): the database layer
is aiosqlite, so the connection+migrations and every boot-time repository
read (settings, policies, profiles, accounts, cooldown restore, plugin
metadata) are awaited here before any manager serves traffic.

The sender cycle (scheduler needs accounts.send_hook; accounts needs the
scheduler for submits/cancels) is broken with a late-bound holder — the same
seam the simulation runner uses.

``build_simulation`` produces a fully in-memory stack over the simulator for
tests, `main.py --sim` and the dashboard's Simulation page.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .api import ControlAPI
from .audit import AuditRecorder
from .config import ProjectConfig, load_config
from .cooldowns import CooldownService
from .credential_service import CredentialService
from .credentials import Credentials
from .event_bus import EventBus
from .logging_service import LogService, ServiceLogger, attach_stdlib_bridge, configure_stderr, detach_stdlib_bridge
from .plugin_manager import PluginManager
from .plugin_state import PluginStateService
from .profile_manager import ProfileManager
from .rate_limit_manager import RateLimitManager
from .settings_manager import SettingsManager
from .task_scheduler import TaskScheduler
from .account_manager import AccountManager

ROOT_LOGGER = logging.getLogger("slaughtermudae")


#: bus events worth a structured log line (MessageReceived is far too chatty)
_LOGGED_EVENTS = (
    "AccountConnected", "AccountDisconnected", "AccountFailed",
    "PluginLoaded", "PluginUnloaded", "PluginEnabledChanged",
    "TaskCompleted", "TaskFailed", "TaskCancelled",
    "RateLimitTriggered", "ScenarioStarted", "ScenarioFinished",
)


def _wire_event_logging(bus: EventBus, logs: LogService) -> None:
    """Record lifecycle events as structured INFO lines (dashboard Logs page)."""
    from .event_bus import Event

    def _on_event(event: Event) -> None:
        if type(event).__name__ not in _LOGGED_EVENTS:
            return
        detail = " ".join(
            f"{key}={value!r}" for key, value in vars(event).items()
            if key not in ("ts", "account")
        )
        logs.record(
            "INFO", "events", f"{type(event).__name__} {detail}".strip(),
            account=getattr(event, "account", None) or None,
            event=type(event).__name__,
        )

    bus.subscribe(Event, _on_event)


async def _seed_config_defaults(limiter: RateLimitManager, config: ProjectConfig,
                                database: Any) -> None:
    """Apply config-file policies ONLY where the DB has none (first boot).

    For each (level, owner, action): if the DB already has a policy, the
    config file's default is skipped — the database is the sole source of
    truth after first boot. This prevents 'ghost' re-added defaults when
    the user has explicitly removed them.
    """
    stored = set()
    try:
        stored = {(p.level, p.owner, p.action) for p in await database.policies.list()}
    except Exception:
        pass  # DB read failure: apply config defaults as a safe fallback
    for action, spec in (config.policies.get("global") or {}).items():
        if ("global", None, action) not in stored:
            interval, jmin, jmax = _policy_triple(action, spec, "global")
            limiter.set_policy_values(action, interval, jmin, jmax, "global", None)
    for account, actions in (config.policies.get("accounts") or {}).items():
        for action, spec in (actions or {}).items():
            if ("account", account, action) not in stored:
                interval, jmin, jmax = _policy_triple(action, spec, f"account {account}")
                limiter.set_policy_values(action, interval, jmin, jmax, "account", account)


def _policy_triple(action: str, spec: Dict[str, Any], where: str) -> tuple:
    """{'interval': 2.0, 'jitter': [0.1, 0.5]} -> (2.0, 0.1, 0.5)."""
    try:
        interval = float(spec["interval"])
        jitter = spec.get("jitter") or [0.0, 0.0]
        jmin, jmax = float(jitter[0]), float(jitter[1])
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ValueError(
            f"policy for {action!r} ({where}) is malformed: {spec!r} "
            "(expected {'interval': seconds, 'jitter': [min, max]})"
        ) from exc
    return interval, jmin, jmax


class Framework:
    """The fully wired stack (managers + api), built by :func:`build`."""

    def __init__(self, api: ControlAPI, bus: EventBus, logs: LogService,
                 limiter: RateLimitManager, scheduler: TaskScheduler,
                 accounts: AccountManager, plugins: PluginManager,
                 profiles: ProfileManager, settings: SettingsManager,
                 audit: AuditRecorder, database: Any,
                 credentials: Credentials, config: ProjectConfig,
                 remote_control: Any = None,
                 credential_service: Any = None,
                 cooldowns: Any = None,
                 plugin_state: Any = None) -> None:
        self.api = api
        self.bus = bus
        self.logs = logs
        self.limiter = limiter
        self.scheduler = scheduler
        self.accounts = accounts
        self.plugins = plugins
        self.profiles = profiles
        self.settings = settings
        self.audit = audit
        self.database = database
        self.credentials = credentials
        self.config = config
        self.remote_control = remote_control
        #: shared services (also wired into the managers/plugin facades)
        self.credential_service = credential_service
        self.cooldowns = cooldowns
        self.plugin_state = plugin_state

    async def shutdown(self) -> None:
        """Graceful: drain the scheduler, stop accounts, detach logging
        sinks, close the database.

        ORDER MATTERS (logging shutdown): the db sink detaches and the
        stdlib bridge is removed BEFORE database.close() — otherwise the
        bridge keeps ingesting stdlib lines into a service whose sink is a
        closed database (every write silently failing forever).
        """
        if self.remote_control is not None:
            self.remote_control.stop()
        await self.scheduler.stop(drain=True)
        await self.accounts.stop_all()
        # QUEUED PERSISTENCE shutdown (agent C contract): drain the log +
        # audit queues BEFORE closing the DB — bounded, never hangs
        await self.logs.stop_persistence()
        await self.audit.stop_persistence()
        detach_stdlib_bridge()             # then stop ingesting stdlib
        await self.database.close()


async def build(
    *,
    env_file: str = ".env",
    config_dir: str = "config",
    db_path: str = "state/slaughter.db",
    plugin_dir: str = "plugins",
    client_factory: Optional[Callable[[str, str], Any]] = None,
    simulation_runner: Any = None,
    credentials: Optional[Credentials] = None,
    database: Any = None,
    quiet: bool = False,
) -> Framework:
    """Compose the live framework (the boot flow of ``main.py``).

    ASYNC: opens the database connection (with migrations) and awaits every
    boot-time repository read before the stack serves anything.
    """
    import time as _time

    if not quiet:
        configure_stderr()
    credentials = credentials or Credentials.from_file(env_file)
    config = load_config(config_dir)

    from database.db import Database

    database = database if database is not None else Database(db_path)
    await database.connect()  # open + pragmas + migrations, FIRST

    # shared services over the database (credentials encrypted at rest,
    # persistent cooldowns, scoped plugin state)
    credential_service = CredentialService(
        database.credentials, key_path=Path("state/credentials.key")
    )
    cooldowns = CooldownService(database.cooldowns)
    plugin_state = PluginStateService(database.kv)

    logs = LogService()
    logs.attach_db(database.logs)
    attach_stdlib_bridge(logs)  # plugin/stdlib logger records -> structured page
    bus = EventBus()
    _wire_event_logging(bus, logs)

    limiter = RateLimitManager(
        bus, ServiceLogger(logs, "ratelimit"), database.policies,
        kv_repo=database.kv,
        lane_enabled=bool((config.policies.get("lane") or {}).get("enabled", False)),
        lane_prefixrate=float((config.policies.get("lane") or {}).get("prefixrate", 1.5)),
    )
    await limiter.load_persisted()  # stored policies + lane config -> memory
    # Config-file policies are FIRST-BOOT DEFAULTS only: they apply solely
    # when no policy exists for that (level, owner, action) in the database.
    # Once the user removes or changes a policy from either surface, the DB
    # is the sole source of truth — the config file never re-adds it.
    await _seed_config_defaults(limiter, config, database)

    settings = SettingsManager(
        bus, ServiceLogger(logs, "settings"),
        plugin_repo=database.plugins, account_settings_repo=database.account_settings,
        global_layer=config.global_settings,
    )
    await settings.load()
    audit = AuditRecorder(database.audit)

    sender_box: Dict[str, Any] = {"send": None}

    async def _sender(task: Any) -> Any:
        hook = sender_box["send"]
        if hook is None:  # no accounts wired yet: structured, non-retryable
            raise RuntimeError("send hook not wired (framework still booting)")
        return await hook(task)

    scheduler = TaskScheduler(
        bus, ServiceLogger(logs, "scheduler"), limiter, _sender,
        audit=audit, history_repo=database.tasks,
    )

    plugins = PluginManager(
        bus, ServiceLogger(logs, "plugins"), settings,
        plugin_repo=database.plugins, plugin_dir=plugin_dir,
        credential_service=credential_service, cooldowns=cooldowns,
        plugin_state=plugin_state,
    )
    plugins.discover()
    await plugins.persist_metadata()

    profiles = ProfileManager(
        database.profiles, ServiceLogger(logs, "profiles"), database.accounts
    )
    await profiles.load()

    if client_factory is None:
        from adapters.discord import RealClientFactory

        client_factory = RealClientFactory(credentials)

    accounts = AccountManager(
        bus, ServiceLogger(logs, "accounts"), database.accounts,
        plugins, profiles, scheduler, settings, audit,
        client_factory, credentials, limiter=limiter,
        api_cache_repo=database.cache,
        credential_service=credential_service, cooldowns=cooldowns,
        plugin_state=plugin_state,
    )
    await accounts.load()  # restore persisted accounts (attach their plugins)
    sender_box["send"] = accounts.send_hook

    # boot recovery for the shared services (before any traffic)
    try:
        await credential_service.refresh()  # status pages know 'db' sources
    except Exception:
        pass
    await cooldowns.restore()

    # config-dir account files (DB rows were restored by accounts.load())
    for payload in config.accounts:
        try:
            await accounts.add_account(
                payload["name"],
                token_ref=payload.get("token_ref"),
                enabled=bool(payload.get("enabled", True)),
                sync_group=payload.get("sync_group"),
                profiles=payload.get("profiles") or None,
            )
        except Exception:
            logs.error("config", "could not register account from config dir",
                       account=payload.get("name"))

    scheduler.start()

    # QUEUED PERSISTENCE startup (agent C contract): the log + audit queues
    # drain to aiosqlite on a worker task — record() never touches SQLite
    # inline, so event paths never block on database I/O
    await logs.start_persistence(database.logs)
    await audit.start_persistence(database.audit)

    if simulation_runner is None:
        try:
            from adapters.simulator import SimulationRunner

            simulation_runner = SimulationRunner(plugin_dir, credentials)
        except Exception:
            simulation_runner = None

    api = ControlAPI(
        accounts=accounts, plugins=plugins, profiles=profiles, settings=settings,
        limiter=limiter, scheduler=scheduler, logs=logs, audit=audit,
        simulation=simulation_runner, credentials=credentials, database=database,
        credentials_svc=credential_service, cooldowns=cooldowns,
        started_at=_time.time(),
    )

    # Discord-side management: owners mention an account (@account status)
    # to run the SAME CoreAPI commands from chat — replies flow through the
    # paced pipeline; REMOTE_CONTROL=0 in .env disables the wiring entirely
    remote_control = None
    if credentials.remote_control_enabled:
        from .remote_control import RemoteControl

        remote_control = RemoteControl(
            bus, scheduler, accounts, lambda: api, credentials,
            ServiceLogger(logs, "remote-control"),
        ).start()
    return Framework(
        api=api, bus=bus, logs=logs, limiter=limiter, scheduler=scheduler,
        accounts=accounts, plugins=plugins, profiles=profiles, settings=settings,
        audit=audit, database=database, credentials=credentials, config=config,
        remote_control=remote_control,
        credential_service=credential_service, cooldowns=cooldowns,
        plugin_state=plugin_state,
    )
