"""Composition root: builds the ENTIRE framework from config + credentials.

Construction order (the only place that knows how the managers interconnect):

  Credentials -> Config -> Database -> LogService(+db sink) -> EventBus
    -> RateLimitManager(+config policies) -> SettingsManager
    -> AuditRecorder -> TaskScheduler(late-bound sender) -> PluginManager
    -> ProfileManager -> AccountManager(restores persisted accounts)
    -> sender bound (accounts.send_hook) -> scheduler.start()
    -> SimulationRunner -> ControlAPI

The sender cycle (scheduler needs accounts.send_hook; accounts needs the
scheduler for submits/cancels) is broken with a late-bound holder — the same
seam the simulation runner uses.

``build_simulation`` produces a fully in-memory stack over the simulator for
tests, `main.py --sim` and the dashboard's Simulation page.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from .api import ControlAPI
from .audit import AuditRecorder
from .config import ProjectConfig, load_config
from .credentials import Credentials
from .event_bus import EventBus
from .logging_service import LogService, ServiceLogger, attach_stdlib_bridge, configure_stderr
from .plugin_manager import PluginManager
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


def _apply_policies(limiter: RateLimitManager, config: ProjectConfig) -> None:
    """Push the config file's policies into the manager (floor still applies)."""
    for action, spec in (config.policies.get("global") or {}).items():
        interval, jmin, jmax = _policy_triple(action, spec, "global")
        limiter.set_policy_values(action, interval, jmin, jmax, "global", None)
    for account, actions in (config.policies.get("accounts") or {}).items():
        for action, spec in (actions or {}).items():
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
                 remote_control: Any = None) -> None:
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

    async def shutdown(self) -> None:
        """Graceful: drain the scheduler, stop accounts, close the database."""
        if self.remote_control is not None:
            self.remote_control.stop()
        await self.scheduler.stop(drain=True)
        await self.accounts.stop_all()
        self.database.close()


def build(
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
    """Compose the live framework (the boot flow of ``main.py``)."""
    import time as _time

    if not quiet:
        configure_stderr()
    credentials = credentials or Credentials.from_file(env_file)
    config = load_config(config_dir)

    from database.db import Database

    database = database if database is not None else Database(db_path)
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
    _apply_policies(limiter, config)

    settings = SettingsManager(
        bus, ServiceLogger(logs, "settings"),
        plugin_repo=database.plugins, account_settings_repo=database.account_settings,
        global_layer=config.global_settings,
    )
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
    )
    plugins.discover()

    profiles = ProfileManager(
        database.profiles, ServiceLogger(logs, "profiles"), database.accounts
    )

    if client_factory is None:
        from adapters.discord import RealClientFactory

        client_factory = RealClientFactory(credentials)

    accounts = AccountManager(
        bus, ServiceLogger(logs, "accounts"), database.accounts,
        plugins, profiles, scheduler, settings, audit,
        client_factory, credentials, limiter=limiter,
        api_cache_repo=database.cache,
    )
    sender_box["send"] = accounts.send_hook

    # config-dir account files (DB rows were restored by AccountManager itself)
    for payload in config.accounts:
        try:
            accounts.add_account(
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
    )
