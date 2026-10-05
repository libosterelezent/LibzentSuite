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

import asyncio
import inspect
import logging
import os
import stat
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .api import ControlAPI
from .audit import AuditRecorder
from .config import ProjectConfig, load_config
from .cooldowns import CooldownService
from .credential_service import CredentialService
from .credentials import Credentials
from .errors import RatePolicyError, UnsafePolicyError
from .event_bus import EventBus
from .logging_service import LogService, ServiceLogger, attach_stdlib_bridge, configure_stderr, detach_stdlib_bridge
from .plugin_manager import PluginManager
from .plugin_state import PluginStateService
from .profile_manager import ProfileManager
from .rate_limit_manager import RateLimitManager
from .settings_manager import SettingsManager
from .task_scheduler import TaskScheduler
from .account_manager import AccountManager
from .account_manager.lifecycle import LifecycleGovernor, LifecyclePolicy

ROOT_LOGGER = logging.getLogger("slaughtermudae")


#: bus events worth a structured log line (MessageReceived is far too chatty)
_LOGGED_EVENTS = frozenset((
    "AccountConnected", "AccountDisconnected", "AccountFailed",
    "PluginLoaded", "PluginUnloaded", "PluginEnabledChanged",
    "TaskCompleted", "TaskFailed", "TaskCancelled",
    "RateLimitTriggered", "ScenarioStarted", "ScenarioFinished",
))


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
    except Exception as exc:
        # DB read failure: apply config defaults as a safe fallback — but
        # say so (silent swallow hid 'why do removed defaults reappear?')
        ROOT_LOGGER.warning(
            "could not read stored policies (%s: %s); applying config "
            "defaults as a fallback", type(exc).__name__, exc)
    removed = getattr(limiter, "removed_default", None)
    if not callable(removed):  # vintage manager shape (attr/property):
        removed = None  # no tombstone knowledge -> seed normally

    def _seed(action, spec, level, owner, where):
        if (level, owner, action) in stored:
            return  # DB owns this key already
        if removed is not None and removed(level, owner, action):
            return  # user explicitly CLEARED it: never resurrect (tombstone)
        interval, jmin, jmax = _policy_triple(action, spec, where)
        try:
            limiter.set_policy_values(action, interval, jmin, jmax, level, owner)
        except UnsafePolicyError as exc:
            # a below-floor config value must not brick the boot — clamp to
            # the floor like load_persisted does for hand-edited DB rows
            ROOT_LOGGER.warning(
                "config default for %s %s clamped to the safety floor (%s)",
                where, action, exc)
            floor = getattr(limiter, "floor_value", 1.5)
            if callable(floor):  # property vs method across manager vintages
                floor = floor()
            try:
                limiter.set_policy_values(action, max(interval, float(floor)),
                                          jmin, jmax, level, owner)
            except RatePolicyError as exc:
                ROOT_LOGGER.warning(
                    "skipping invalid config default for %s %s (%s)",
                    where, action, exc)
        except RatePolicyError as exc:
            # a malformed spec (e.g. an inverted jitter pair) must not brick
            # the whole boot either — skip THIS policy with a warning,
            # consistent with the floor clamp above
            ROOT_LOGGER.warning(
                "skipping invalid config default for %s %s (%s)",
                where, action, exc)

    for action, spec in (config.policies.get("global") or {}).items():
        _seed(action, spec, "global", None, "global")
    for account, actions in (config.policies.get("accounts") or {}).items():
        for action, spec in (actions or {}).items():
            _seed(action, spec, "account", account, f"account {account}")


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
        self._shut_down = False

    async def shutdown(self) -> None:
        """Graceful: drain the scheduler, stop accounts, detach logging
        sinks, close the database. Idempotent and FAULT-TOLERANT: every step
        runs even if an earlier one raised, and the database is ALWAYS
        closed (a failing ``accounts.stop_all`` used to skip the log drain
        and leak the connection thread, which can hang interpreter exit).

        A CancelledError inside one step is recorded like any failure —
        the remaining steps (crucially database.close) still run; the
        cancellation re-raises at the END so the caller still sees it.

        ORDER MATTERS (bus drain): ``accounts.stop_all`` publishes tail
        events (AccountDisconnected "stopped") via ``publish_nowait``;
        the bus drain runs AFTER it so those orphans are actually
        dispatched (and reach the still-running log-persistence queue)
        instead of being cancelled by loop close. The scheduler still
        drains BEFORE the accounts stop — queued tasks complete while
        the senders are alive; one drain at the end covers both
        backlogs (drain re-checks the queue until stable).

        ORDER MATTERS (logging shutdown): the db sink detaches and the
        stdlib bridge is removed BEFORE database.close() — otherwise the
        bridge keeps ingesting stdlib lines into a service whose sink is a
        closed database (every write silently failing forever).
        """
        if self._shut_down:
            return
        self._shut_down = True
        steps = (
            ("remote control", self._stop_remote_control),
            ("scheduler", lambda: self.scheduler.stop(drain=True)),
            ("accounts", self.accounts.stop_all),
            # ROUND-4 (F7/1): drain AFTER stop_all — AccountDisconnected
            # tail events publish after the account teardown and were
            # previously never awaited (the drain had already returned)
            ("bus drain", self.bus.drain),
            # QUEUED PERSISTENCE shutdown (agent C contract): drain the log
            # + audit queues BEFORE closing the DB — bounded, never hangs
            ("log persistence", self.logs.stop_persistence),
            ("audit persistence", self.audit.stop_persistence),
            ("stdlib bridge", self._detach_bridge),
            ("plugin manager", self._close_plugins),
            ("database", self.database.close),
        )
        interrupted: Optional[BaseException] = None
        for label, step in steps:
            try:
                outcome = step()
                if inspect.iscoroutine(outcome):
                    await outcome
            except asyncio.CancelledError as exc:
                if interrupted is None:
                    interrupted = exc
                    ROOT_LOGGER.warning(
                        "shutdown step %r was cancelled (continuing teardown)",
                        label)
            except KeyboardInterrupt as exc:  # a second Ctrl-C / the
                if interrupted is None:  # dashboard's exit signal landing
                    interrupted = exc  # mid-teardown must NOT skip the
                    ROOT_LOGGER.warning(  # remaining steps (db.close!)
                        "shutdown step %r interrupted (continuing teardown)",
                        label)
            except Exception as exc:
                ROOT_LOGGER.warning("shutdown step %r failed: %s: %s",
                                    label, type(exc).__name__, exc)
        if interrupted is not None:
            raise interrupted

    async def _stop_remote_control(self) -> None:
        if self.remote_control is not None:
            self.remote_control.stop()

    def _close_plugins(self) -> None:
        close = getattr(self.plugins, "close", None)
        if close is not None:
            close()

    @staticmethod
    async def _detach_bridge() -> None:
        detach_stdlib_bridge()  # then stop ingesting stdlib


def _env_reconcile_interval(default: float = 30.0) -> float:
    """SM_RECONCILE_INTERVAL_S (seconds; 0 = reconcile every status call).

    Round-4 tunability: operators stretching a busy dashboard beyond the
    30s default (or tightening it for a second-process edit to surface
    faster) no longer need a code change. Malformed/negative values warn
    and fall back to the default — boot never fails on an env typo.
    """
    raw = os.environ.get("SM_RECONCILE_INTERVAL_S")
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        ROOT_LOGGER.warning(
            "SM_RECONCILE_INTERVAL_S=%r is not a number; using the "
            "default %.0fs", raw, default)
        return default
    if value < 0:
        ROOT_LOGGER.warning(
            "SM_RECONCILE_INTERVAL_S=%r is negative; using the default "
            "%.0fs", raw, default)
        return default
    return value


async def _security_audit(database: Any, credential_service: Any,
                          env_file: str) -> None:
    """FINAL-PHASE (F7): boot-time setup security pass (warn-only).

    1. Permission audit: the DB, its WAL sidecar, the credential key and
       the .env must not be group/world-readable (POSIX only — different
       platforms genuinely have different permission semantics; we warn,
       never fail boot, and never claim parity).
    2. Key/DB mismatch canary: try to decrypt ONE stored ciphertext — a
       wrong/restored-from-backup master key surfaces HERE as one clear
       warning instead of per-account decrypt errors at first use.
    """
    if os.name == "posix":
        for label, path in (
            ("database", getattr(database, "_path", None)),
            ("credential key",
             str(getattr(credential_service, "_key_path", "") or "")),
            ("env file", env_file),
        ):
            if not path or path == ":memory:":
                continue
            try:
                mode = stat.S_IMODE(os.stat(path).st_mode)
                if mode & 0o077:
                    ROOT_LOGGER.warning(
                        "insecure permissions %04o on %s (%s) — "
                        "chmod 600 recommended", mode, path, label)
            except OSError:
                continue
    # mismatch canary: one decrypt probe over the stored accounts
    probe = getattr(credential_service, "_probe_decrypt", None)
    if callable(probe):
        try:
            await probe()
        except Exception:
            pass  # the probe warns internally; boot never fails on it


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
    credential_key_path: Optional[Path] = None,
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

    # PARTIAL-BUILD CLEANUP: anything failing from here on must release what
    # was already started (DB connection thread, log/audit drain workers,
    # stdlib bridge, lanes, remote control) — otherwise a failed boot leaks
    # them and a real aiosqlite connection can keep the process from exiting
    cleanups: list = []
    try:
        return await _assemble(
            cleanups, _time, credentials, config, database,
            plugin_dir=plugin_dir, client_factory=client_factory,
            simulation_runner=simulation_runner,
            credential_key_path=credential_key_path,
            env_file=env_file, db_path=db_path)
    except BaseException:
        for label, undo in reversed(cleanups):
            try:
                outcome = undo()
                if inspect.iscoroutine(outcome):
                    await outcome
            except asyncio.CancelledError:
                ROOT_LOGGER.warning(
                    "build cleanup step %r was cancelled (continuing)", label)
            except KeyboardInterrupt:
                ROOT_LOGGER.warning(
                    "build cleanup step %r interrupted (continuing)", label)
            except Exception as exc:
                ROOT_LOGGER.warning("build cleanup step %r failed: %s: %s",
                                    label, type(exc).__name__, exc)
        await _safe_close(database)
        raise


async def _safe_close(database: Any) -> None:
    try:
        await database.close()
    except Exception:
        pass


async def _assemble(cleanups: list, _time: Any, credentials: Credentials,
                    config: ProjectConfig, database: Any, *, plugin_dir: str,
                    client_factory: Optional[Callable[[str, str], Any]],
                    simulation_runner: Any,
                    credential_key_path: Optional[Path] = None,
                    env_file: str = ".env",
                    db_path: str = "state/slaughter.db") -> Framework:
    """Wire every manager over an already-connected database."""

    # shared services over the database (credentials encrypted at rest,
    # persistent cooldowns, scoped plugin state)
    # The master key lives NEXT TO THE DATABASE FILE, not the CWD: a
    # different --db path (or another working directory) used to mint a
    # FRESH key at ./state/credentials.key and every stored token became
    # undecryptable. The default db path resolves to the same file as the
    # old CWD-relative default — zero migration.
    credential_service = CredentialService(
        database.credentials,
        # The master key lives NEXT TO THE DATABASE FILE, not the CWD: a
        # different --db path (or another working directory) used to mint a
        # FRESH key at ./state/credentials.key and every stored token became
        # undecryptable. The default db path resolves to the same file as the
        # old CWD-relative default — zero migration. An EXPLICIT key path
        # (benchmark isolation) wins over the derived one.
        key_path=(
            credential_key_path
            if credential_key_path is not None
            else Path(db_path).expanduser().absolute().parent / "credentials.key"
        ),
    )
    cooldowns = CooldownService(database.cooldowns)
    plugin_state = PluginStateService(database.kv)

    logs = LogService()
    logs.attach_db(database.logs)
    # QUEUED PERSISTENCE startup (agent C contract): start the drain BEFORE
    # the boot logging begins — records made earlier never persist. record()
    # only enqueues, so it never blocks on SQLite
    await logs.start_persistence(database.logs)
    cleanups.append(("log persistence", logs.stop_persistence))
    attach_stdlib_bridge(logs)  # plugin/stdlib logger records -> structured page
    cleanups.append(("stdlib bridge", detach_stdlib_bridge))
    bus = EventBus()
    _wire_event_logging(bus, logs)

    limiter = RateLimitManager(
        bus, ServiceLogger(logs, "ratelimit"), database.policies,
        kv_repo=database.kv,
        lane_enabled=bool((config.policies.get("lane") or {}).get("enabled", False)),
        lane_prefixrate=(config.policies.get("lane") or {}).get("prefixrate", 1.5),
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
    await audit.start_persistence(database.audit)
    cleanups.append(("audit persistence", audit.stop_persistence))

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
    # task ids continue after the archived ones (they used to restart at 1
    # every boot, colliding with old rows; the swallowed insert error meant
    # a restarted process archived none of its tasks)
    await scheduler.restore_sequence()
    cleanups.append(("scheduler", lambda: scheduler.stop(drain=False)))

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

        command_prefix = getattr(credentials, "self_command_prefix", "")
        if command_prefix:
            # ROUND-4 (F7/8): a set prefix with no owner ids is a fully
            # silent misconfiguration — every self-command fails the
            # owner check. Mirrors the REMOTE_CONTROL boot warning.
            if not getattr(credentials, "owner_user_ids", None):
                ROOT_LOGGER.warning(
                    "SELF_COMMAND_PREFIX is set but OWNER_USER_IDS is "
                    "empty — the account's own user id must be listed in "
                    "OWNER_USER_IDS or every self-command will be "
                    "rejected (see core/credentials.py)")
            if command_prefix == "@":
                # bare '@' collides with EVERY mention, not just the
                # account's own — degenerate, warn loudly
                ROOT_LOGGER.warning(
                    "SELF_COMMAND_PREFIX='@' matches every mention, not "
                    "just the account's own — use a distinct prefix or "
                    "'@me' for the own-mention form")
        client_factory = RealClientFactory(
            credentials, command_prefix=command_prefix)

    accounts = AccountManager(
        bus, ServiceLogger(logs, "accounts"), database.accounts,
        plugins, profiles, scheduler, settings, audit,
        client_factory, credentials, limiter=limiter,
        api_cache_repo=database.cache,
        credential_service=credential_service, cooldowns=cooldowns,
        plugin_state=plugin_state,
        lifecycle=LifecycleGovernor(),  # PHASE-4 (S4): anti-thrash defaults
    )
    cleanups.append(("accounts", accounts.stop_all))
    await accounts.load()  # restore persisted accounts (attach their plugins)
    sender_box["send"] = accounts.send_hook

    # boot recovery for the shared services (before any traffic)
    try:
        await credential_service.refresh()  # status pages know 'db' sources
    except Exception as exc:
        ROOT_LOGGER.warning(
            "could not read stored-credential accounts (%s: %s); status "
            "pages may show credentials as missing until the next refresh",
            type(exc).__name__, exc)
    await _security_audit(database, credential_service, env_file)
    await cooldowns.restore()

    # config-dir account files (DB rows were restored by accounts.load())
    for payload in config.accounts:
        try:
            raw_enabled = payload.get("enabled", True)
            if isinstance(raw_enabled, str):
                raw_enabled = raw_enabled.strip().lower() in ("true", "1", "yes", "on")
            await accounts.add_account(
                payload["name"],
                token_ref=payload.get("token_ref"),
                enabled=bool(raw_enabled),
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
        credentials_svc=credential_service, cooldowns=cooldowns,
        started_at=_time.time(),
        reconcile_interval_s=_env_reconcile_interval(),
    )

    # Discord-side management: owners mention an account (@account status)
    # to run the SAME CoreAPI commands from chat — replies flow through the
    # paced pipeline; REMOTE_CONTROL=0 in .env disables the wiring entirely
    remote_control = None
    if credentials.remote_control_enabled:
        from .remote_control import RemoteControl

        owner_ids = getattr(credentials, "owner_user_ids", None)
        if not owner_ids:  # fail loudly at boot, not per-mention
            ROOT_LOGGER.warning(
                "REMOTE_CONTROL is enabled but OWNER_USER_IDS is empty or "
                "malformed — remote control will reject every mention")
        else:
            ROOT_LOGGER.info("remote control armed for %s owner id(s)",
                             len(owner_ids))
        remote_control = RemoteControl(
            bus, scheduler, accounts, lambda: api, credentials,
            ServiceLogger(logs, "remote-control"),
        ).start()
        # PHASE-4 (S3-G2): let the account-removal cascade clear the
        # remote-control reply-throttle stamp for the removed name.
        accounts._remote_control = remote_control
        cleanups.append(("remote control", remote_control.stop))
    return Framework(
        api=api, bus=bus, logs=logs, limiter=limiter, scheduler=scheduler,
        accounts=accounts, plugins=plugins, profiles=profiles, settings=settings,
        audit=audit, database=database, credentials=credentials, config=config,
        remote_control=remote_control,
        credential_service=credential_service, cooldowns=cooldowns,
        plugin_state=plugin_state,
    )
