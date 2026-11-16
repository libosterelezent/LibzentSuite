"""Composition root: builds the ENTIRE framework from config + credentials.

Construction order (the only place that knows how the managers interconnect):

  Credentials -> Config -> Database(await connect) -> LogService(+db sink)
    -> EventBus -> RateLimitManager(+config policies, await load_persisted)
    -> SyncGroupManager(await load) -> SettingsManager(await load)
    -> AuditRecorder
    -> TaskScheduler(late-bound sender) -> PluginManager(discover + await
    persist_metadata) -> managers
    -> AccountManager(await load; restores persisted accounts + delegates
       group membership restore to SyncGroupManager)
    -> CredentialService(+refresh)/CooldownService(await restore)/
       PluginStateService wired into the managers
    -> sender bound (accounts.send_hook) -> scheduler.start()
    -> SimulationRunner -> ControlAPI

``build`` is ASYNC (``framework = await build(...)``): the database layer
is aiosqlite, so the connection+migrations and every boot-time repository
read (settings, policies, accounts, cooldown restore, plugin
metadata) are awaited here before any manager serves traffic.

The sender cycle (scheduler needs accounts.send_hook; accounts needs the
scheduler for submits/cancels) is broken with a late-bound holder — the same
seam the simulation runner uses.

``build_simulation`` produces a fully in-memory stack over the simulator for
tests, `main.py --sim` and the dashboard's Simulation page.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .account_manager import AccountManager
from .account_manager.lifecycle import LifecycleGovernor
from .api import ControlAPI
from .audit import AuditRecorder
from .config import ProjectConfig, load_config
from .cooldowns import CooldownService
from .credential_service import CredentialService
from .credentials import Credentials
from .errors import RatePolicyError, UnsafePolicyError
from .event_bus import EventBus
from .logging_service import (
    LogService,
    ServiceLogger,
    attach_stdlib_bridge,
    claim_stderr_pipeline,
    detach_stdlib_bridge,
    release_stderr_pipeline,
)
from .plugin_manager import COMMAND_HUB, PluginManager
from .plugin_state import PluginStateService
from .rate_limit_manager import RateLimitManager
from .settings_manager import SettingsManager
from .task_scheduler import TaskScheduler
from .workspace.service import WorkspaceService
from .workspace.signals import SignalOutbox


class _LatePluginsInvoker:
    """Resolves the plugin manager at CALL time: the W4 assembly runs
    before the manager is constructed, and handlers only ever invoke
    long after boot completed."""

    def __init__(self, resolve: Any) -> None:
        self._resolve = resolve

    async def __call__(
        self, account: str, plugin: str, handler: str, payload: dict[str, Any]
    ) -> Any:
        return await self._resolve().invoke_handler(account, plugin, handler, payload)


class _WorkspaceGateReader:
    """Check-7 facts: plugin assigned/enabled on the account, the
    step's gate condition against the LIVE settings view, channel
    binding usability from the registry (only available resolves)."""

    def __init__(self, database: Any) -> None:
        self._db = database
        self._plugins: Any = None

    def bind_plugins(self, plugins: Any) -> None:
        self._plugins = plugins

    async def plugin_gate_facts(
        self, account: str, plugin: str, gate: dict[str, Any]
    ) -> dict[str, Any]:
        assigned = plugin in set(await self._db.account_plugins.plugins_of(account))
        enabled = True
        if assigned:
            row = await self._db.connection.fetch_one(
                "SELECT enabled FROM plugin_states WHERE account=? AND plugin=?",
                (account, plugin),
            )
            enabled = bool(row["enabled"]) if row is not None else True
        gate_passed = True
        setting = str(gate.get("setting") or "")
        if setting and self._plugins is not None:
            live = getattr(self._plugins, "live_settings", None)
            if callable(live):
                values = live(account, plugin) or {}
                expected = gate.get("equals")
                gate_passed = setting in values and values[setting] == expected
        binding_usable = True
        binding_reason = ""
        if assigned:
            bindings = await self._db.account_channels.plugin_bindings(account, plugin)
            if bindings:
                usable = []
                for binding in bindings:
                    row = await self._db.connection.fetch_one(
                        "SELECT validity FROM account_channels WHERE account=? AND channel_id=?",
                        (account, binding.channel_id),
                    )
                    validity = row["validity"] if row is not None else "unknown"
                    usable.append(validity == "available")
                binding_usable = any(usable)
                if not binding_usable:
                    binding_reason = "no binding resolves to an available channel"
        return {
            "plugin_assigned": assigned,
            "plugin_enabled": enabled,
            "gate_passed": gate_passed,
            "channel_binding_usable": binding_usable,
            "channel_binding_reason": binding_reason,
        }


if TYPE_CHECKING:
    from collections.abc import Callable

ROOT_LOGGER = logging.getLogger("libzent")


#: bus events worth a structured log line (MessageReceived is far too chatty)
_LOGGED_EVENTS = frozenset(
    (
        "AccountConnected",
        "AccountDisconnected",
        "AccountFailed",
        "PluginLoaded",
        "PluginUnloaded",
        "PluginEnabledChanged",
        "TaskCompleted",
        "TaskFailed",
        "TaskCancelled",
        "RateLimitTriggered",
        "ScenarioStarted",
        "ScenarioFinished",
    )
)


def _wire_event_logging(bus: EventBus, logs: LogService) -> None:
    """Record lifecycle events as structured INFO lines (dashboard Logs page)."""
    from .event_bus import Event

    def _on_event(event: Event) -> None:
        if type(event).__name__ not in _LOGGED_EVENTS:
            return
        detail = " ".join(
            f"{key}={value!r}" for key, value in vars(event).items() if key not in ("ts", "account")
        )
        logs.record(
            "INFO",
            "events",
            f"{type(event).__name__} {detail}".strip(),
            account=getattr(event, "account", None) or None,
            event=type(event).__name__,
        )

    bus.subscribe(Event, _on_event)


async def _seed_config_defaults(
    limiter: RateLimitManager, config: ProjectConfig, database: Any
) -> None:
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
            "could not read stored policies (%s: %s); applying config defaults as a fallback",
            type(exc).__name__,
            exc,
        )
    removed = getattr(limiter, "removed_default", None)
    if not callable(removed):  # vintage manager shape (attr/property):
        removed = None  # no tombstone knowledge -> seed normally

    def _seed(action: str, spec: dict[str, Any], level: str, owner: str | None, where: str) -> None:
        if (level, owner, action) in stored:
            return  # DB owns this key already
        if removed is not None and removed(level, owner, action):
            return  # user explicitly CLEARED it: never resurrect (tombstone)
        try:
            interval, jmin, jmax = _policy_triple(action, spec, where)
            limiter.set_policy_values(action, interval, jmin, jmax, level, owner)
        except UnsafePolicyError as exc:
            # a below-floor config value must not brick the boot — clamp to
            # the floor like load_persisted does for hand-edited DB rows
            ROOT_LOGGER.warning(
                "config default for %s %s clamped to the safety floor (%s)", where, action, exc
            )
            floor = getattr(limiter, "floor_value", 1.5)
            if callable(floor):  # property vs method across manager vintages
                floor = floor()
            try:
                limiter.set_policy_values(
                    action, max(interval, float(floor)), jmin, jmax, level, owner
                )
            except RatePolicyError as exc:
                ROOT_LOGGER.warning(
                    "skipping invalid config default for %s %s (%s)", where, action, exc
                )
        except ValueError as exc:
            # a malformed spec (e.g. interval "fast") must not brick the
            # boot either — skip THIS policy with a warning, matching the
            # floor clamp above (RatePolicyError is a ValueError too)
            ROOT_LOGGER.warning(
                "skipping invalid config default for %s %s (%s)", where, action, exc
            )

    for action, spec in (config.policies.get("global") or {}).items():
        _seed(action, spec, "global", None, "global")
    for account, actions in (config.policies.get("accounts") or {}).items():
        for action, spec in (actions or {}).items():
            _seed(action, spec, "account", account, f"account {account}")


def _policy_triple(action: str, spec: dict[str, Any], where: str) -> tuple[float, float, float]:
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

    def __init__(
        self,
        api: ControlAPI,
        bus: EventBus,
        logs: LogService,
        limiter: RateLimitManager,
        scheduler: TaskScheduler,
        accounts: AccountManager,
        plugins: PluginManager,
        settings: SettingsManager,
        audit: AuditRecorder,
        database: Any,
        credentials: Credentials,
        config: ProjectConfig,
        credential_service: Any = None,
        cooldowns: Any = None,
        plugin_state: Any = None,
        workspaces: Any = None,
        plugin_data: Any = None,
        cooldown_resync_stop: Callable[[], Any] | None = None,
        claims_stderr: bool = False,
    ) -> None:
        self.api = api
        self.bus = bus
        self.logs = logs
        self.limiter = limiter
        self.scheduler = scheduler
        self.accounts = accounts
        self.plugins = plugins
        self.settings = settings
        self.audit = audit
        self.database = database
        self.credentials = credentials
        self.config = config
        #: shared services (also wired into the managers/plugin facades)
        self.credential_service = credential_service
        self.cooldowns = cooldowns
        self.plugin_state = plugin_state
        #: W3 workspace orchestration (the one writer)
        self.workspaces = workspaces
        #: PHASE 10 AccountPluginData store (B3)
        self.plugin_data = plugin_data
        #: stops the periodic cooldown-resync task (set by _assemble)
        self._cooldown_resync_stop = cooldown_resync_stop

        # this framework holds one CLAIM on the process-global stderr
        # pipeline — shutdown releases it (the last release stops)
        self._claims_stderr = claims_stderr
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

        ORDER MATTERS (logging shutdown): the db sink detaches and the
        stdlib bridge is removed BEFORE database.close() — otherwise the
        bridge keeps ingesting stdlib lines into a service whose sink is a
        closed database (every write silently failing forever).
        """
        if self._shut_down:
            return
        self._shut_down = True
        steps = [
            ("scheduler", lambda: self.scheduler.stop(drain=True)),
            ("cooldown resync", self._stop_cooldown_resync),
            # stop the reconciler's periodic pass BEFORE the drains so its
            ("bus drain", self.bus.drain),
            ("accounts", self.accounts.stop_all),
            # SECOND drain: stop_all emits AccountDisconnected (and plugin
            # teardown emits PluginUnloaded) tail events — dropping them
            # left the dashboard's Logs page without its own shutdown
            # narrative and straggling subscribers mid-teardown
            ("bus drain (tail)", self.bus.drain),
            # QUEUED PERSISTENCE shutdown (agent C contract): drain the log
            # + audit queues BEFORE closing the DB — bounded, never hangs
            ("log persistence", self.logs.stop_persistence),
            ("audit persistence", self.audit.stop_persistence),
            ("stdlib bridge", self._detach_bridge),
            ("plugin manager", self._close_plugins),
            # the store is a SECOND aiosqlite connection: leaving it open
            # leaks a non-daemon worker thread that hangs interpreter exit
            ("plugin-data store", self._close_plugin_data),
            ("database", self.database.close),
        ]
        if self._claims_stderr:
            # LAST: release this framework's claim on the process-global
            # pipeline (the LAST release stops it) — placing it after the
            # steps above keeps their final warnings on the console
            steps.append(("stderr pipeline", release_stderr_pipeline))
        ordered = tuple(steps)
        interrupted: BaseException | None = None
        for label, step in ordered:
            try:
                outcome = step()
                if inspect.iscoroutine(outcome):
                    await outcome
            except asyncio.CancelledError as exc:
                if interrupted is None:
                    interrupted = exc
                    ROOT_LOGGER.warning(
                        "shutdown step %r was cancelled (continuing teardown)", label
                    )
            except KeyboardInterrupt as exc:  # a second Ctrl-C / the
                if interrupted is None:  # dashboard's exit signal landing
                    interrupted = exc  # mid-teardown must NOT skip the
                    ROOT_LOGGER.warning(  # remaining steps (db.close!)
                        "shutdown step %r interrupted (continuing teardown)", label
                    )
            except Exception as exc:
                ROOT_LOGGER.warning(
                    "shutdown step %r failed: %s: %s", label, type(exc).__name__, exc
                )
        if interrupted is not None:
            raise interrupted

    def _close_plugins(self) -> None:
        close = getattr(self.plugins, "close", None)
        if close is not None:
            close()

    async def _stop_cooldown_resync(self) -> None:
        stop = self._cooldown_resync_stop
        if stop is None:
            return
        self._cooldown_resync_stop = None  # idempotent
        try:
            outcome = stop()
            if inspect.iscoroutine(outcome):
                await outcome
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ROOT_LOGGER.warning("cooldown resync shutdown failed: %s: %s", type(exc).__name__, exc)

    async def _close_plugin_data(self) -> None:
        if self.plugin_data is not None:
            await self.plugin_data.close()

    @staticmethod
    async def _detach_bridge() -> None:
        detach_stdlib_bridge()  # then stop ingesting stdlib


async def _security_audit(database: Any, credential_service: Any, env_file: str) -> None:
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
            ("credential key", str(getattr(credential_service, "_key_path", "") or "")),
            ("env file", env_file),
        ):
            if not path or path == ":memory:":
                continue
            try:
                mode = stat.S_IMODE(os.stat(path).st_mode)
                if mode & 0o077:
                    ROOT_LOGGER.warning(
                        "insecure permissions %04o on %s (%s) — chmod 600 recommended",
                        mode,
                        path,
                        label,
                    )
            except OSError:
                continue
    # mismatch canary: one decrypt probe over the stored accounts
    probe = getattr(credential_service, "_probe_decrypt", None)
    if callable(probe):
        try:
            await probe()
        except Exception as exc:
            # the probe handles its own expected failures; anything that
            # still escapes must be VISIBLE, not double-swallowed
            ROOT_LOGGER.warning(
                "boot decrypt canary raised %s: %s (key misconfiguration must not be silent)",
                type(exc).__name__,
                exc,
            )


async def build(
    *,
    env_file: str = ".env",
    config_dir: str = "config",
    db_path: str = "state/libzent.db",
    plugin_dir: str = "plugins",
    client_factory: Callable[[str, str], Any] | None = None,
    simulation_runner: Any = None,
    credentials: Credentials | None = None,
    database: Any = None,
    quiet: bool = False,
    credential_key_path: Path | None = None,
) -> Framework:
    """Compose the live framework (the boot flow of ``main.py``).

    ASYNC: opens the database connection (with migrations) and awaits every
    boot-time repository read before the stack serves anything.
    """
    import time as _time

    # CLAIM (review pass-3 #1, wiring fixed pass-4 #1/#2): every
    # non-quiet build registers as a USER of the process-global pipeline
    # — creating it when idle, borrowing it when live; the listener
    # stops only when the LAST live claim releases. The claim is HELD
    # regardless of whether this build created the pipeline (the call's
    # return value only reports creation — gating the release on it let
    # a borrower increment the count and never release it), and the
    # release is registered BEFORE any fallible step so an early boot
    # failure cannot leak the claim.
    cleanups: list[tuple[str, Any]] = []
    if not quiet:
        claim_stderr_pipeline()
        cleanups.append(("stderr pipeline", release_stderr_pipeline))
    stderr_claimed = not quiet
    try:
        credentials = credentials or Credentials.from_file(env_file)
        config = load_config(config_dir)

        from database.db import Database

        database = database if database is not None else Database(db_path)
        await database.connect()  # open + pragmas + migrations, FIRST
        return await _assemble(
            cleanups,
            _time,
            credentials,
            config,
            database,
            plugin_dir=plugin_dir,
            client_factory=client_factory,
            simulation_runner=simulation_runner,
            credential_key_path=credential_key_path,
            env_file=env_file,
            db_path=db_path,
            stderr_claimed=stderr_claimed,
        )
    except BaseException:
        for label, undo in reversed(cleanups):
            try:
                outcome = undo()
                if inspect.iscoroutine(outcome):
                    await outcome
            except asyncio.CancelledError:
                ROOT_LOGGER.warning("build cleanup step %r was cancelled (continuing)", label)
            except KeyboardInterrupt:
                ROOT_LOGGER.warning("build cleanup step %r interrupted (continuing)", label)
            except Exception as exc:
                ROOT_LOGGER.warning(
                    "build cleanup step %r failed: %s: %s", label, type(exc).__name__, exc
                )
        await _safe_close(database)
        raise


async def _safe_close(database: Any) -> None:
    with contextlib.suppress(Exception):
        await database.close()


async def _assemble(
    cleanups: list[tuple[str, Any]],
    _time: Any,
    credentials: Credentials,
    config: ProjectConfig,
    database: Any,
    *,
    plugin_dir: str,
    client_factory: Callable[[str, str], Any] | None,
    simulation_runner: Any,
    credential_key_path: Path | None = None,
    env_file: str = ".env",
    stderr_claimed: bool = False,
    db_path: str = "state/libzent.db",
) -> Framework:
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
    # PHASE 10 (B3, LOCKED): AccountPluginData — its OWN WAL SQLite file
    # beside the main database, self-initializing schema (separate
    # database flow by design); exposed to plugins ONLY through the
    # per-(account, plugin) services facade, never directly
    from database.plugin_data import PluginDataStore

    plugin_data_store = PluginDataStore(
        Path(db_path).expanduser().absolute().parent / "plugin-data.db"
    )
    await plugin_data_store.connect()
    cleanups.append(("plugin-data store", plugin_data_store.close))

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

    # DEPRECATED: the engine no longer reads the lane constructor kwargs —
    # runtime lane state comes from load_persisted()'s kv read. Warn once
    # when a config file still carries a truthy lane section so operators
    # can clean it up, and stop feeding the dead values forward.
    lane_cfg = config.policies.get("lane") or {}
    if lane_cfg.get("enabled") or lane_cfg.get("prefixrate"):
        ROOT_LOGGER.warning(
            "config rate_limits.json: the 'lane' section is deprecated "
            "and ignored by the rate engine (enabled=%r prefixrate=%r) — "
            "manage the send lane from the dashboard/commands instead; "
            "remove the section to silence this warning",
            lane_cfg.get("enabled"),
            lane_cfg.get("prefixrate"),
        )
    limiter = RateLimitManager(
        bus,
        ServiceLogger(logs, "ratelimit"),
        database.policies,
        kv_repo=database.kv,
    )
    await limiter.load_persisted()  # stored policies + lane config -> memory
    # Config-file policies are FIRST-BOOT DEFAULTS only: they apply solely
    # when no policy exists for that (level, owner, action) in the database.
    # Once the user removes or changes a policy from either surface, the DB
    # is the sole source of truth — the config file never re-adds it.
    await _seed_config_defaults(limiter, config, database)

    # W3: the workspace service replaces SyncGroupManager + FleetService
    # (one writer, no seam closures — WORKSPACE_DESIGN). The v9 modules
    # stay importable and tested until W5 deletes them.
    # W4: the durable signal outbox — acceptance persists BEFORE the
    # emission acknowledges; delivery sweeps ride the reconciler loop
    # (the chain runtime consumes matched signals idempotently by id)
    signals_outbox = SignalOutbox(
        database.signals,
        deliver=None,  # the chain runtime's intake wires in below
    )

    # C2: the cog host — the converged plugin command face. The
    # toolbox seams: to_rich is the adapter's converter (imported at
    # composition — plugins never import adapters), audit publishes the
    # typed MessageReceived on the bus (the identical row the intake
    # records), the inbound factory is the event itself.
    from adapters.model import convert_message
    from core.event_bus import MessageReceived
    from core.plugin_manager.cog_host import PluginCogHost

    def _publish_inbound(event: Any) -> None:
        bus.publish_nowait(event)

    def _to_rich(message: Any) -> Any:
        """The C1 to_rich seam, wired the way the client's own intake
        derives the flag: convert_message REQUIRES ``dm`` — passing the
        bare function would TypeError on every call (found by the
        mypy-strict pass, 2026-11-15)."""
        import discord

        channel = getattr(message, "channel", None)
        return convert_message(message, dm=isinstance(channel, discord.DMChannel))

    cog_host = PluginCogHost(
        to_rich=_to_rich,
        audit_publisher=_publish_inbound,
        inbound_factory=lambda **kw: MessageReceived(**kw),
    )

    workspaces = WorkspaceService(
        database.workspaces,
        preset_defs=database.workspace_presets,  # P8: definitions + executions
        profiles=database.account_plugin_profiles,  # P8: profile references
        accounts=database.accounts,
        channels_repo=database.account_channels,
        presets_repo=database.presets,
        provenance=database.account_plugins,
        plugin_data=plugin_data_store,
        settings_writer=database.plugins,
        limiter=limiter,
        publisher=lambda ev: ROOT_LOGGER.debug("workspace event: %s %s", ev.kind, ev.workspace),
    )
    await workspaces.load()
    await workspaces.sync_runtime()

    # W4: the chain runtime (trigger intake + run engine) and the
    # dispatch guard. Terminal events flow from the bus; handler
    # invocation rides the plugin manager's per-account contexts; the
    # check-7 reader answers plugin-assigned/gate/binding facts.
    from core.event_bus import TaskCompleted, TaskFailed
    from core.workspace.guard import WorkspaceDispatchGuard, WorkspaceReconciler
    from core.workspace.runtime import ChainRuntime, TerminalTrigger

    # (the plugin manager binds below, once constructed — gate
    # resolution needs its live settings view)
    _workspace_gate_reader = _WorkspaceGateReader(database)

    chain_runtime = ChainRuntime(
        database.workspaces,
        service=workspaces,
        outbox=signals_outbox,
        signal_reader=database.signals,
        invoker=_LatePluginsInvoker(lambda: plugins),  # resolved at call time
        gate_reader=_workspace_gate_reader,
    )

    async def _on_task_completed(event: TaskCompleted) -> None:
        with contextlib.suppress(Exception):
            await chain_runtime.on_terminal(
                TerminalTrigger(
                    account=event.account,
                    plugin=event.plugin,
                    action=str(getattr(event, "action", "") or ""),
                    kind="task_completed",
                )
            )

    async def _on_task_failed(event: TaskFailed) -> None:
        with contextlib.suppress(Exception):
            await chain_runtime.on_terminal(
                TerminalTrigger(
                    account=event.account,
                    plugin=event.plugin,
                    action=str(getattr(event, "action", "") or ""),
                    kind="task_failed",
                )
            )

    bus.subscribe(TaskCompleted, _on_task_completed)
    bus.subscribe(TaskFailed, _on_task_failed)

    workspace_reconciler = WorkspaceReconciler(
        workspaces,
        runtime=chain_runtime,
        outbox=signals_outbox,
        plugin_data=plugin_data_store,
        channels_repo=database.account_channels,
    )
    await workspace_reconciler.reconcile()
    workspace_reconciler.start()
    cleanups.append(("workspace reconciler", workspace_reconciler.stop))

    settings = SettingsManager(
        bus,
        ServiceLogger(logs, "settings"),
        plugin_repo=database.plugins,
        account_settings_repo=database.account_settings,
        global_layer=config.global_settings,
    )
    await settings.load()
    audit = AuditRecorder(database.audit)
    await audit.start_persistence(database.audit)
    cleanups.append(("audit persistence", audit.stop_persistence))

    sender_box: dict[str, Any] = {"send": None}

    async def _sender(task: Any) -> Any:
        hook = sender_box["send"]
        if hook is None:  # no accounts wired yet: structured, non-retryable
            raise RuntimeError("send hook not wired (framework still booting)")
        return await hook(task)

    scheduler = TaskScheduler(
        bus,
        ServiceLogger(logs, "scheduler"),
        limiter,
        _sender,
        audit=audit,
        history_repo=database.tasks,
    )
    #: forward declaration — the late-plugins invoker below closes over
    # ``plugins`` before the manager is constructed (resolved at call time)
    plugins: PluginManager
    # AccessManager: the admin panel's store (admins, per-account command
    # perms, prefixes, primary bot id) over the kv repo; one-time env migration
    # seeds admins from OWNER_USER_IDS when the stored list is empty.
    from .access_manager import AccessManager

    access = AccessManager(database.kv, credentials=credentials)
    await access.migrate_from_credentials()
    await access.warm_prefix_cache()
    COMMAND_HUB.access = access
    # task ids continue after the archived ones (they used to restart at 1
    # every boot, colliding with old rows; the swallowed insert error meant
    # a restarted process archived none of its tasks)
    await scheduler.restore_sequence()
    cleanups.append(("scheduler", lambda: scheduler.stop(drain=False)))

    plugins = PluginManager(
        bus,
        ServiceLogger(logs, "plugins"),
        settings,
        plugin_repo=database.plugins,
        plugin_dir=plugin_dir,
        credential_service=credential_service,
        cooldowns=cooldowns,
        plugin_state=plugin_state,
    )
    # C2: the cog host rides the plugin manager (host_cog/unhost_cog
    # drive add_cog/remove_cog per (account, plugin))
    plugins.cog_host = cog_host
    # the self-command executor's command gate reads this attribute; it
    # was never set here, so framework verbs fell back to the RETIRED env
    # OWNER_USER_IDS grant instead of the DB admin panel
    plugins._access_manager = access
    cleanups.append(("plugins", plugins.close))
    plugins.discover()
    # Plugin dashboard UIs register at BOOT (discovery time) so plugin
    # pages are visible in the dashboard immediately — not only after an
    # account connects. Class-level instantiation, no account needed.
    await plugins.persist_metadata()
    # W4: the gate reader resolves chain-step gates through the live
    # settings view — bound now that the manager exists
    _workspace_gate_reader.bind_plugins(plugins)
    # the dispatch guard needs the scheduler (and its halt state) —
    # wired here, after both sides of the seam exist
    scheduler.set_dispatch_guard(WorkspaceDispatchGuard(workspaces))

    if client_factory is None:
        from adapters.discord import RealClientFactory

        client_factory = RealClientFactory(
            credentials,
            command_prefix=getattr(credentials, "self_command_prefix", ""),
            access_manager=access,
            audit_recorder=audit,
        )

    accounts = AccountManager(
        bus,
        ServiceLogger(logs, "accounts"),
        database.accounts,
        plugins,
        scheduler,
        settings,
        audit,
        client_factory,
        credentials,
        limiter=limiter,
        api_cache_repo=database.cache,
        credential_service=credential_service,
        cooldowns=cooldowns,
        plugin_state=plugin_state,
        plugin_data=plugin_data_store,  # PHASE 10: data_* plugin API
        lifecycle=LifecycleGovernor(),  # PHASE-4 (S4): anti-thrash defaults
        signals=signals_outbox,
        profile_store=database.account_plugin_profiles,  # P3: profile lifecycle
        account_plugins=database.account_plugins,  # capability source
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
            type(exc).__name__,
            exc,
        )
    await _security_audit(database, credential_service, env_file)
    # lifecycle-safe restore: the account registry is the ownership
    # metadata — a write that raced an account's deletion cascade left
    # an orphan row, and it is swept here rather than resurrected
    _known = {a.name for a in await database.accounts.list()}
    await cooldowns.restore(known_accounts=_known)

    # lightweight periodic cooldown resync (30s): external edits (a second
    # process or a manual DB change) heal within one period even in
    # headless processes where nothing polls ControlAPI.status().
    # Defensive by design — failures are logged, never fatal, and shutdown
    # is signalled via the event so the loop never outlives the framework.
    cooldown_resync_stop_evt = asyncio.Event()

    async def _cooldown_resync_loop() -> None:
        while not cooldown_resync_stop_evt.is_set():
            try:
                await asyncio.wait_for(cooldown_resync_stop_evt.wait(), timeout=30.0)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await cooldowns.resync()
            except asyncio.CancelledError:
                raise
            except Exception:
                ROOT_LOGGER.debug("periodic cooldown resync failed", exc_info=True)

    cooldown_resync_task = asyncio.create_task(_cooldown_resync_loop())

    async def _stop_cooldown_resync_task() -> None:
        cooldown_resync_stop_evt.set()
        if not cooldown_resync_task.done():
            cooldown_resync_task.cancel()
            try:
                await cooldown_resync_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                ROOT_LOGGER.warning(
                    "cooldown resync task teardown failed: %s: %s", type(exc).__name__, exc
                )

    cleanups.append(("cooldown resync", _stop_cooldown_resync_task))

    # config-dir account files (DB rows were restored by accounts.load())
    # ``plugins`` is the capability key; a legacy ``profiles`` key in a
    # config file is deliberately IGNORED (profiles no longer decide
    # capabilities — nothing is assigned, nothing breaks)
    discovered_plugins = set(plugins.names())
    for payload in config.accounts:
        try:
            raw_enabled = payload.get("enabled", True)
            if isinstance(raw_enabled, str):
                raw_enabled = raw_enabled.strip().lower() in ("true", "1", "yes", "on")
            config_plugins = payload.get("plugins") or []
            unknown = [p for p in config_plugins if p not in discovered_plugins]
            if unknown:
                # typo trap (same convention as the api layer): refuse the
                # whole file, loudly — a silently-unmatched plugin name
                # would read as "created" while attaching nothing
                raise ValueError(
                    f"unknown plugin(s) {unknown} (known plugins: {sorted(discovered_plugins)})"
                )
            await accounts.add_account(
                payload["name"],
                token_ref=payload.get("token_ref"),
                enabled=bool(raw_enabled),
                workspace=payload.get("workspace"),
                plugins=config_plugins,
            )
        except Exception:
            logs.error(
                "config", "could not register account from config dir", account=payload.get("name")
            )

    scheduler.start()

    if simulation_runner is None:
        try:
            from adapters.simulator import SimulationRunner

            simulation_runner = SimulationRunner(plugin_dir, credentials)
        except Exception:
            simulation_runner = None

    api = ControlAPI(
        accounts=accounts,
        plugins=plugins,
        settings=settings,
        access=access,
        limiter=limiter,
        scheduler=scheduler,
        logs=logs,
        audit=audit,
        simulation=simulation_runner,
        credentials=credentials,
        database=database,
        credentials_svc=credential_service,
        cooldowns=cooldowns,
        workspaces=workspaces,
        plugin_data=plugin_data_store,
        started_at=_time.time(),
    )

    # Framework management from chat (status/accounts/plugins/ratelimit/
    # lane/tasks/estop/simulation) rides FrameworkVerbsCog: the hub's
    # api_provider bridges the verbs to the ONE ControlAPI. The mention
    # bridge (RemoteControl) retired with the hub's registry — see dump/.
    COMMAND_HUB.api_provider = lambda: api

    return Framework(
        api=api,
        bus=bus,
        logs=logs,
        limiter=limiter,
        scheduler=scheduler,
        accounts=accounts,
        plugins=plugins,
        settings=settings,
        audit=audit,
        database=database,
        credentials=credentials,
        config=config,
        workspaces=workspaces,
        plugin_data=plugin_data_store,
        credential_service=credential_service,
        cooldowns=cooldowns,
        plugin_state=plugin_state,
        cooldown_resync_stop=_stop_cooldown_resync_task,
        claims_stderr=stderr_claimed,
    )
