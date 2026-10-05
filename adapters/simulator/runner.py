"""SimulationRunner — a COMPLETE ephemeral stack per scenario run (spec §16).

Wiring follows the pinned constructors in INTEGRATION.md exactly (async
boot order, same as the production bootstrap)::

    await Database(":memory:").connect()
    CredentialService(db.credentials, key_path=<TEMP KEY PER RUN>) /
        CooldownService(db.cooldowns) / PluginStateService(db.kv)
        <- synthetic creds, restore()d
    EventBus()
    LogService() + await start_persistence(db.logs)   (queued db sink)
    SettingsManager(bus, logger, plugin_repo=db.plugins,
                    account_settings_repo=db.account_settings,
                    global_layer=DEMO_PLUGIN_SETTINGS) + await load()
    RateLimitManager(bus, logger, db.policies, kv_repo=db.kv,
                     lane_enabled=False, lane_prefixrate=1.5)
        + await load_persisted()
    AuditRecorder(db.audit) + await start_persistence(db.audit)
    TaskScheduler(bus, logger, limiter, sender)    <- LATE-BOUND SENDER (below)
        + await restore_sequence()
    PluginManager(bus, logger, settings, db.plugins, plugin_dir)
        + discover() + await persist_metadata()
    ProfileManager(db.profiles, logger, db.accounts) + await load()
    AccountManager(bus, logger, db.accounts, plugins_mgr, profiles, scheduler,
                   settings, audit, client_factory, credentials[, limiter],
                   credential_service=..., cooldowns=..., plugin_state=...)
        + await load()

HERMETICITY (audit round 3): the ephemeral CredentialService gets a TEMP
key file per run (``tempfile.mkdtemp``, removed in teardown) — the default
key path is the CWD-relative ``state/credentials.key``, so sim runs used
to lazily CREATE/read the PRODUCTION master key.

LANE WIRING (audit round 3): the RateLimitManager is built with the kv
repo and explicit lane args like production, but the lane is DISABLED —
a disabled lane is a 0s pass-through, so scenario pacing is unchanged.
Scenarios can flip it live through ``ScenarioAPI.limiter``
(set_lane_enabled / set_lane_prefixrate); before this the lane machinery
was absent from every sim run and lane pause was dead code in sims.

TEARDOWN ORDER (audit round 3): matches Framework.shutdown — remote
control, scheduler drain (queued tasks complete while the account can
still send), bus drain, accounts stop, log/audit persistence stops,
bridge detach, plugin close, database close — plus the sim-only cleanups
(sys.modules purge of this run's plugin modules, temp key removal).

RESIDUAL GAP (documented, deliberate): production's
``_wire_event_logging`` (bus lifecycle events -> structured 'events' log
lines) is bootstrap-only; sim reports therefore lack TaskCompleted /
RateLimitTriggered / similar event lines. Wiring it here would duplicate
the bootstrap's process-wide subscription semantics for little scenario
value — scenarios assert on the audit trail and world journal instead.

LIFECYCLE GOVERNOR (round-4): the AccountManager is built with a REAL
``LifecycleGovernor`` (production parity — sims used to run unlimited, so
start/stop thrashing was untestable). Verified scenario-safe: estop and
teardown stops bypass the governor BY DESIGN (``_bypass_lifecycle=True``
skips both the gate and the cooldown-commit), so estop_halt's
stop→resume→start sequence never trips pacing, and no scenario's plain
lifecycle ops come close to the 5-start/5s bucket or the 10s restart
floor. The governor is exposed on ``ScenarioAPI.lifecycle``.

RECONCILE CADENCE (round-4): the sim ControlAPI is built with
``reconcile_interval_s=2.0`` (production default 30s) so the periodic
re-reconciliation pass fires inside a scenario's lifetime instead of
being untestable. RESIDUAL GAP: the sim ControlAPI still gets no
``cooldowns`` service, so its periodic pass reconciles account records
only (cooldown resync stays bootstrap-side).

SELF-COMMAND PREFIX (round-4): the client factory threads a
``command_prefix`` into every SimClient, mirroring the live bootstrap
(``RealClientFactory(credentials, command_prefix=...)``). The source is
the same credentials object the runner already resolves — a scenario may
override it via ``Scenario.self_command_prefix`` (the self_command
scenario uses "sm!"); default is off, exactly like SELF_COMMAND_PREFIX.
When a prefix is ACTIVE the runner also appends the account's own
synthetic id to OWNER_USER_IDS — the live layer's documented requirement
(the re-fired trigger carries the account's own id as author and must
pass the plugin owner check).

THE LATE-BOUND SENDER SEAM (documented for the integrator): the scheduler
needs the account manager's ``send_hook`` as its sender, but the account
manager needs the scheduler in its own constructor. The cycle is broken with
a late-bound holder — ``sender = lambda task: box["send"](task)`` — where
``box["send"]`` is filled with ``accounts.send_hook`` immediately after the
AccountManager exists (before ``scheduler.start()``, so no task can ever run
against an unfilled box).

Manager modules from A3/A5/A6 are imported INSIDE run(): when one is still
pending integration, run() raises SimulationError with a human-readable
message instead of an ImportError traceback.

Documented contract decisions (ambiguous seams — see INTEGRATION.md
ground rules; the lead reconciles at integration):

- LOGGER SHAPE: the spine SettingsManager and the A3/A6 managers call the
    injected logger in STDLIB style (``logger.info("fmt %s", arg)``,
    ``logger.exception(...)``), while A4's rate-limit engine calls it in
    LogService style (``logger.info(source, message)``) and A5's scheduler
    auto-detects. This runner therefore injects :class:`ServiceLogger` — a
    tiny stdlib-logger-shaped adapter over ``LogService.record`` — into every
    manager. Both call styles produce a readable structured entry (so
    ``log_lines`` and the dashboard Logs page keep working), and a logging
    failure can never break a manager.

- ACCOUNT MANAGER ``limiter`` KEYWORD: passed when accepted (A7 assignment
  wording); the INTEGRATION.md pinned signature omits it, so a TypeError
  falls back to the keyword-less construction — either variant works.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from adapters.simulator.client import SimClient
from adapters.simulator.scenarios import (
    ACCOUNT_NAME,
    DEMO_PLUGIN_SETTINGS,
    SCENARIOS,
    Scenario,
    ScenarioAPI,
    quiet_window,
)
from adapters.simulator.world import (
    SYNTHETIC_ALPHA_ID,
    SYNTHETIC_GUILD_CHANNEL,
    SYNTHETIC_MUDAE_ID,
    SYNTHETIC_OWNER_ID,
    SimWorld,
)
from core.credentials import Credentials
from core.errors import SimulationError
from core.event_bus import EventBus, ScenarioFinished, ScenarioStarted
from core.logging_service import ServiceLogger, LogService
from core.settings_manager import SettingsManager

#: safety floor fallback when the limiter module constant is unavailable
_FALLBACK_FLOOR = 1.5

#: hard cap for one scenario's script + settle + checks; a hung scenario
#: becomes a failed report instead of a run that never returns
_SCENARIO_TIMEOUT_S = 180.0

#: grace for a cancelled scenario task to actually stop before the runner
#: abandons it (the 180s cap + this grace is the absolute worst case)
_CANCEL_GRACE_S = 5.0

#: sim lane baseline: the machinery is WIRED (kv repo + explicit args, like
#: production) but DISABLED — a disabled lane is a 0s pass-through, so
#: scenario pacing is identical to a lane-less stack. Scenarios flip it
#: live via ScenarioAPI.limiter.
_SIM_LANE_PREFIXRATE = 1.5

#: sim reconcile cadence (round-4): production's 30s default never fires
#: inside a scenario's lifetime; 2s makes the periodic re-reconciliation
#: observable (a second status() >2s into a scenario re-reads records)
_SIM_RECONCILE_INTERVAL_S = 2.0

#: cap on the report's log_lines (dashboard payload bounded); a spec-§9
#: shaped notice replaces the dropped older lines
_LOG_LINES_CAP = 2000

#: prefix of the plugin loader's synthetic parent module names (see
#: core.plugin_manager.loader). The runner purges the ones ITS discover
#: created so repeated sim runs do not grow sys.modules forever.
_PLUGIN_MODULE_PREFIX = "_mudae_plugroot"


def _reap_scenario_task(task: "asyncio.Task[None]") -> None:
    """Retrieve an abandoned scenario task's outcome (no GC warning)."""
    if not task.cancelled():
        task.exception()


def _purge_plugin_modules(names: "set[str]") -> None:
    """Pop this run's loader-created plugin modules from sys.modules.

    The sim's single-use PluginLoader never rescans, so its own
    stale-parent purge never fires — every run leaked one generation of
    ``_mudae_plugroot_N`` modules (~21KB measured). The runner diffs
    sys.modules around ITS discover call and pops exactly those entries
    (loader-private state is never touched). Classes and instances stay
    functional — only the import registry entries go, the same contract
    as the loader's own purge.
    """
    for name in names:
        sys.modules.pop(name, None)



def _entry_dict(entry: Any) -> dict:
    """Normalize one audit/log entry to a plain dict (A5 shape-agnostic)."""
    if isinstance(entry, dict):
        return dict(entry)
    as_dict = getattr(entry, "as_dict", None)
    if callable(as_dict):
        try:
            return dict(as_dict())
        except Exception:
            pass
    data = getattr(entry, "__dict__", None)
    if isinstance(data, dict) and data:
        return dict(data)
    return {"repr": repr(entry)}


def _load_stack_modules() -> SimpleNamespace:
    """Import the manager modules built by the other agents (inside run()).

    Raises SimulationError — with the missing module named — while any piece
    of the stack is still pending integration.
    """
    try:
        from core.account_manager import AccountManager
        from core.account_manager.lifecycle import LifecycleGovernor
        from core.audit import AuditRecorder
        from core.cooldowns import CooldownService
        from core.credential_service import CredentialService
        from core.plugin_manager import PluginManager
        from core.plugin_state import PluginStateService
        from core.profile_manager import ProfileManager
        from core.rate_limit_manager import MIN_ACTION_INTERVAL, RateLimitManager
        from core.task_scheduler import TaskScheduler
        from database.db import Database
    except (ImportError, AttributeError) as exc:
        # AttributeError covers half-landed namespace packages (a directory
        # without __init__.py imports "fine" but exports nothing)
        raise SimulationError(
            f"simulation stack unavailable - a core module is still pending "
            f"integration: {exc}"
        ) from exc
    return SimpleNamespace(
        AccountManager=AccountManager,
        LifecycleGovernor=LifecycleGovernor,
        AuditRecorder=AuditRecorder,
        CooldownService=CooldownService,
        CredentialService=CredentialService,
        PluginManager=PluginManager,
        PluginStateService=PluginStateService,
        ProfileManager=ProfileManager,
        RateLimitManager=RateLimitManager,
        TaskScheduler=TaskScheduler,
        Database=Database,
        floor=float(MIN_ACTION_INTERVAL),
    )


class SimulationRunner:
    """Builds and tears down a full in-memory stack for each scenario run."""

    def __init__(
        self,
        plugin_dir: str = "plugins",
        credentials: Optional[Credentials] = None,
    ) -> None:
        """Args:
            plugin_dir: plugin directory the PluginManager discovers.
            credentials: fleet credentials; None builds synthetic sim
                credentials (custom credentials must define
                ``DISCORD_TOKEN_ALPHA`` for the sim account to start).
        """
        self._plugin_dir = str(plugin_dir)
        self._credentials = credentials

    # ------------------------------------------------------------- listing
    def list(self) -> List[dict]:
        """[{name, description}] for every registered scenario."""
        return [
            {"name": scenario.name, "description": scenario.description}
            for scenario in SCENARIOS.values()
        ]

    def scenario(self, name: str) -> Scenario:
        """Scenario lookup; raises SimulationError for unknown names."""
        scenario = SCENARIOS.get(name)
        if scenario is None:
            known = ", ".join(sorted(SCENARIOS)) or "(none)"
            raise SimulationError(f"unknown scenario {name!r} (known: {known})")
        return scenario

    # ----------------------------------------------------------------- run
    async def run(self, name: str) -> dict:
        """Run one scenario on a fresh ephemeral stack; returns the report.

        The report: {"name", "passed", "duration_s", "failures",
        "log_lines", "interactions", "summary"}. Scenario scripts never
        propagate exceptions — a crash becomes a failure string.
        """
        scenario = self.scenario(name)
        run_start = time.time()
        mods = _load_stack_modules()

        # --- world (scenario topology + the account's own synthetic user)
        world = SimWorld()
        try:
            scenario.world_setup(world)
        except Exception as exc:
            raise SimulationError(
                f"scenario {name!r} world_setup failed: {exc}"
            ) from exc
        world.add_user(SYNTHETIC_ALPHA_ID, ACCOUNT_NAME)

        # --- infrastructure (pinned constructors, INTEGRATION.md)
        try:
            db = mods.Database(":memory:")
            await db.connect()
        except Exception as exc:
            raise SimulationError(
                f"scenario {name!r}: in-memory database failed: {exc}"
            ) from exc

        # shared services over the ephemeral database, wired exactly like
        # the production bootstrap so scenarios exercise them.
        #
        # HERMETICITY: an explicit TEMP master-key file per run — the
        # service default is the CWD-relative state/credentials.key, which
        # sim runs must never create or read (that is the PRODUCTION key).
        key_dir = tempfile.mkdtemp(prefix="sm-sim-cred-")
        try:
            credential_service = mods.CredentialService(
                db.credentials,
                key_path=os.path.join(key_dir, "credentials.key"),
            )
        except BaseException:
            shutil.rmtree(key_dir, ignore_errors=True)  # ctor failure must
            raise  # not leak the temp key dir past the main teardown
        cooldowns = mods.CooldownService(db.cooldowns)
        plugin_state = mods.PluginStateService(db.kv)

        bus = EventBus()
        logger = LogService()
        # LOGGER SEAM (documented in the module docstring): managers mix
        # stdlib-style and LogService-style calls — inject stdlib-shaped
        # adapters over LogService.record so both conventions work AND the
        # structured entries still flow into log_lines / the Logs page.
        log_settings = ServiceLogger(logger, "settings")
        log_ratelimit = ServiceLogger(logger, "ratelimit")
        log_scheduler = ServiceLogger(logger, "scheduler")
        log_plugins = ServiceLogger(logger, "plugins")
        log_profiles = ServiceLogger(logger, "profiles")
        log_accounts = ServiceLogger(logger, "accounts")
        settings = SettingsManager(
            bus,
            log_settings,
            plugin_repo=db.plugins,
            account_settings_repo=db.account_settings,
            global_layer=DEMO_PLUGIN_SETTINGS,
        )
        remote = None
        audit = None  # bound inside the assembly try (partial-failure teardown)
        #: loader modules created by THIS run's discover (sys.modules purge
        #: set — see _purge_plugin_modules)
        sim_plugin_modules: "set[str]" = set()
        try:  # assembly guard: a failure below must close the ephemeral DB
            await settings.load()
            # LANE WIRED, DISABLED (production args, zero timing impact):
            # enter() is a 0s pass-through while the gate is off, so every
            # scenario paces exactly as before — but the lane machinery is
            # present and scenarios can toggle it via ScenarioAPI.limiter.
            limiter = mods.RateLimitManager(
                bus, log_ratelimit, db.policies,
                kv_repo=db.kv,
                lane_enabled=False,
                lane_prefixrate=_SIM_LANE_PREFIXRATE,
            )
            await limiter.load_persisted()
            audit = mods.AuditRecorder(db.audit)
            # QUEUED PERSISTENCE (production parity): drain workers for the
            # log/audit rows BEFORE any traffic; stopped in teardown BEFORE
            # db.close(). record() only enqueues, so scenario timing is
            # unaffected (measured: no material duration shift).
            await logger.start_persistence(db.logs)
            await audit.start_persistence(db.audit)

            # LATE-BOUND SENDER SEAM — see module docstring
            box: Dict[str, Callable[[Any], Any]] = {}

            async def sender(task: Any) -> Any:
                return await box["send"](task)

            scheduler = mods.TaskScheduler(
                bus, log_scheduler, limiter, sender,
                audit=audit, history_repo=db.tasks,
            )
            # production parity: task ids continue after the archived ones
            # (one :memory: high-water read at boot — no timing impact)
            await scheduler.restore_sequence()

            plugins_mgr = mods.PluginManager(
                bus, log_plugins, settings, db.plugins, self._plugin_dir,
                credential_service=credential_service, cooldowns=cooldowns,
                plugin_state=plugin_state,
            )
            pre_discover_modules = frozenset(sys.modules)
            try:
                plugins_mgr.discover()
                await plugins_mgr.persist_metadata()
            except Exception as exc:
                raise SimulationError(
                    f"scenario {name!r}: plugin discovery failed in "
                    f"{self._plugin_dir!r}: {exc}"
                ) from exc
            finally:
                # modules the loader's synthetic parents pulled into
                # sys.modules during THIS discover (empty prefix match
                # when discovery failed before creating any)
                sim_plugin_modules = {
                    module_name
                    for module_name in set(sys.modules) - pre_discover_modules
                    if module_name.startswith(_PLUGIN_MODULE_PREFIX)
                }

            profiles = mods.ProfileManager(db.profiles, log_profiles, db.accounts)
            await profiles.load()
            await cooldowns.restore()

            # --- credentials: SYNTHETIC ids overlaid on the fleet values
            # (fleet credentials carry real OWNER/MUDAE ids, which would
            # silently break owner-gating and Mudae-page matching in every
            # scenario)
            sim_overlay = {
                "MUDAE_BOT_ID": str(SYNTHETIC_MUDAE_ID),
                "OWNER_USER_IDS": str(SYNTHETIC_OWNER_ID),
                f"DISCORD_TOKEN_{ACCOUNT_NAME.upper()}": f"sim-token-{ACCOUNT_NAME}",
            }
            values = (
                {**self._credentials.snapshot(), **sim_overlay}
                if self._credentials is not None
                else dict(sim_overlay)
            )
            # SELF-COMMAND PARITY (round-4): a scenario's declared prefix
            # wins over the fleet value; an ACTIVE prefix also appends the
            # account's own synthetic id to OWNER_USER_IDS — the live
            # layer's documented requirement (the re-fired trigger carries
            # the account's own id as author and must pass the owner check)
            # Hermeticity (round-4 re-audit A): sims honor ONLY the
            # scenario's explicit prefix — the LIVE .env's
            # SELF_COMMAND_PREFIX must not leak into dashboard-driven runs
            # (it would silently activate the layer + rewrite owner lists
            # for unrelated scenarios). Default "" = off, like a live
            # boot with the key unset.
            self_command_prefix = scenario.self_command_prefix or ""
            if self_command_prefix:
                values["SELF_COMMAND_PREFIX"] = self_command_prefix
            if self_command_prefix:
                values["OWNER_USER_IDS"] = (
                    f"{SYNTHETIC_OWNER_ID},{SYNTHETIC_ALPHA_ID}"
                )
            credentials = Credentials.from_values(values)

            user_ids = {ACCOUNT_NAME: SYNTHETIC_ALPHA_ID}

            def client_factory(account_name: str, token: str) -> SimClient:
                # command_prefix mirrors the live factory's threading of
                # SELF_COMMAND_PREFIX (default "" = the layer is off)
                return SimClient(
                    world,
                    user_ids[account_name],
                    seed=SYNTHETIC_ALPHA_ID,
                    command_prefix=self_command_prefix,
                )

            # import the account's synthetic token into the ephemeral credential
            # store so the DB-first resolution path is the one scenarios exercise
            try:
                await credential_service.store(
                    ACCOUNT_NAME, f"sim-token-{ACCOUNT_NAME}"
                )
            except Exception:
                pass

            # LIFECYCLE GOVERNOR (round-4): production-parity pacing for
            # start/stop — estop/teardown stops bypass it by design (see
            # module docstring), so no scenario needs a permissive policy
            governor = mods.LifecycleGovernor()

            accounts = self._build_account_manager(
                mods, bus, log_accounts, db, plugins_mgr, profiles, scheduler,
                settings, audit, client_factory, credentials, limiter,
                credential_service=credential_service, cooldowns=cooldowns,
                plugin_state=plugin_state, lifecycle=governor,
            )
            await accounts.load()
            box["send"] = accounts.send_hook  # fill the seam BEFORE start()

            # a real ControlAPI + the mention-prefix bridge, so scenarios can
            # exercise Discord-side management exactly like production
            from core.api import ControlAPI

            api = ControlAPI(
                accounts=accounts, plugins=plugins_mgr, profiles=profiles,
                settings=settings, limiter=limiter, scheduler=scheduler,
                logs=logger, audit=audit, credentials=credentials,
                # RECONCILE (round-4): 2s cadence so the periodic pass is
                # observable inside a scenario's lifetime (production
                # default 30s never fires; see module docstring)
                reconcile_interval_s=_SIM_RECONCILE_INTERVAL_S,
            )
            from core.remote_control import RemoteControl

            if not getattr(credentials, "remote_control_enabled", True):
                remote = None  # REMOTE_CONTROL=0: opt out here too, not
            else:  # just in production — parity with bootstrap wiring
                remote = RemoteControl(
                    bus, scheduler, accounts, lambda: api, credentials,
                    ServiceLogger(logger, "remote-control"),
                ).start()
        except BaseException as exc:
            # partial-assembly teardown (mirrors the main finally's steps
            # for everything that can exist at this point)
            for stopper in (logger.stop_persistence,
                            getattr(audit, "stop_persistence", None)):
                if stopper is None:
                    continue
                try:
                    await stopper()
                except Exception:
                    pass
            _purge_plugin_modules(sim_plugin_modules)
            shutil.rmtree(key_dir, ignore_errors=True)
            await db.close()
            if isinstance(
                exc,
                (SimulationError, KeyboardInterrupt, SystemExit,
                 asyncio.CancelledError),
            ):
                raise
            raise SimulationError(
                f"scenario {name!r}: stack assembly failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        # --- observability subscriptions BEFORE any traffic can flow
        log_lines: List[str] = []

        def _on_log(entry: Any) -> None:
            """Spec §9 line: elapsed + severity + source + message [+event]."""
            try:
                elapsed = max(0.0, float(getattr(entry, "ts", 0.0) or 0.0) - run_start)
            except (TypeError, ValueError):
                elapsed = 0.0
            line = (
                f"{elapsed:08.3f}s {str(getattr(entry, 'severity', 'INFO')):<7} "
                f"{getattr(entry, 'source', '?')} {getattr(entry, 'message', '')}"
            )
            event = getattr(entry, "event", None)
            if event:
                line += f" [{event}]"
            log_lines.append(line)

        interactions: List[dict] = []

        def _on_audit(entry: Any) -> None:
            interactions.append(_entry_dict(entry))

        unsub_log = logger.subscribe(_on_log)
        unsub_audit = audit.subscribe(_on_audit)

        failures: List[str] = []
        account_started = False
        scheduler_started = False
        # stdlib ingestion for THIS stack: standalone runs previously DROPPED
        # plugin/loader INFO lines (tree effective level WARNING with no
        # bridge). Attach one bound to the run's service when the tree has
        # none; when the live stack's bridge is present (dashboard-sim) keep
        # it — swapping a process-global handler mid-run would misroute the
        # MAIN stack's lines — and document the cross-leak instead.
        from core.logging_service import StdlibBridge, attach_stdlib_bridge, detach_stdlib_bridge

        tree_root = logging.getLogger("slaughtermudae")
        bridge_attached_here = not any(
            isinstance(h, StdlibBridge) for h in tree_root.handlers
        )
        if bridge_attached_here:
            attach_stdlib_bridge(logger)
        else:
            logger.record(
                "INFO", "sim",
                "shared stdlib bridge detected: raw stdlib lines from this run "
                "land in the main log service (documented cross-leak)",
            )
        try:
            # --- stack phase: bring the ephemeral stack up
            try:
                await bus.publish(ScenarioStarted(account="", name=name))
                scheduler.start()
                scheduler_started = True
                await accounts.add_account(ACCOUNT_NAME)
                await accounts.start(ACCOUNT_NAME)
                account_started = True

                # scenario policies, CLAMPED to the safety floor: sims never
                # configure unsafe pacing (the limiter would reject it anyway)
                floor = max(
                    _FALLBACK_FLOOR, float(getattr(mods, "floor", _FALLBACK_FLOOR))
                )
                for action, spec in (scenario.policies or {}).items():
                    interval = max(floor, float(spec.get("interval", floor)))
                    jitter_min, jitter_max = spec.get("jitter", (0.0, 0.0))
                    limiter.set_policy_values(
                        action, interval, float(jitter_min), float(jitter_max)
                    )
            except Exception as exc:  # a broken stack is a failed report
                failures.append(f"stack phase failed: {type(exc).__name__}: {exc}")

            # --- scenario phase: drive the flow + post-mortem checks
            if not failures:
                facade = ScenarioAPI(
                    world=world,
                    accounts=accounts,
                    channel_id=SYNTHETIC_GUILD_CHANNEL,
                    owner_id=SYNTHETIC_OWNER_ID,
                    account_user_id=SYNTHETIC_ALPHA_ID,
                    mudae_id=SYNTHETIC_MUDAE_ID,
                    owner_says=(
                        lambda channel_id, content, author_id:
                        world.deliver(channel_id, author_id, content)
                    ),
                    audit_entries=lambda: list(interactions),
                    settings_overrides=DEMO_PLUGIN_SETTINGS,
                    remote=remote,
                    limiter=limiter,
                    scheduler=scheduler,
                    control=api,
                    lifecycle=governor,
                )
                async def _scenario_phase() -> None:
                    await scenario.script(facade)
                    # one extra quiet window for stragglers after the script
                    await world.wait_for_quiet(
                        SYNTHETIC_GUILD_CHANNEL, quiet=quiet_window(),
                        limit=90.0,
                    )
                    failures.extend(scenario.checks(list(interactions), world))

                # NOT wait_for: a script that swallows CancelledError once
                # makes wait_for return SUCCESS after the budget expired —
                # a hung scenario would report passed. A task + bounded wait
                # makes the deadline authoritative whatever the script does.
                scenario_task = asyncio.get_running_loop().create_task(
                    _scenario_phase()
                )
                try:
                    await asyncio.wait(
                        {scenario_task}, timeout=_SCENARIO_TIMEOUT_S
                    )
                    overrun = not scenario_task.done()
                    if overrun:
                        scenario_task.cancel()
                        await asyncio.wait(
                            {scenario_task}, timeout=_CANCEL_GRACE_S
                        )
                    if overrun or scenario_task.cancelled():
                        failures.append(
                            f"scenario timed out after {_SCENARIO_TIMEOUT_S:.0f}s "
                            f"(hung script or unsettled world)"
                        )
                        if not scenario_task.done():
                            failures.append(
                                "scenario task ignored cancellation - "
                                "abandoned mid-flight"
                            )
                            scenario_task.add_done_callback(_reap_scenario_task)
                    else:
                        crash = scenario_task.exception()
                        if crash is not None:
                            if isinstance(crash, Exception):
                                failures.append(
                                    f"scenario crashed: "
                                    f"{type(crash).__name__}: {crash}"
                                )
                            else:
                                raise crash
                except BaseException:
                    if not scenario_task.done():
                        scenario_task.cancel()
                    raise

        finally:
            world.cancel_pending_replies()
            # PRODUCTION TEARDOWN ORDER (Framework.shutdown, audit round 3):
            # the scheduler drains FIRST (queued tasks complete while the
            # account can still send), THEN the accounts stop. The old
            # order killed the senders before the queued work, so a drain
            # could never deliver what it waited for.
            try:
                if remote is not None:
                    remote.stop()
            except Exception:
                pass
            if scheduler_started:
                try:
                    await scheduler.stop(drain=True)
                except Exception as exc:
                    failures.append(f"scheduler stop failed: {exc}")
            if account_started:
                try:
                    await accounts.stop(ACCOUNT_NAME)
                except Exception as exc:
                    failures.append(f"account stop failed: {exc}")
            try:
                # AFTER accounts stop (production Framework.shutdown order,
                # round-4): stop_all publishes AccountDisconnected tail
                # events — draining first would strand them
                await bus.drain()
            except Exception:
                pass  # bounded (2s default); teardown must never hang here
            # persistence queues stop BEFORE the database closes (agent C
            # contract) — a queued row must never drain into a dead DB
            for label, stopper in (
                ("log persistence", logger.stop_persistence),
                ("audit persistence", audit.stop_persistence),
            ):
                try:
                    await stopper()
                except Exception as exc:
                    failures.append(f"{label} stop failed: {exc}")
            unsub_audit()
            unsub_log()
            try:
                if bridge_attached_here:
                    detach_stdlib_bridge()  # EVERY exit path: a cancelled run
            except Exception:  # must not leak the process-global bridge
                pass
            try:
                plugins_close = getattr(plugins_mgr, "close", None)
                if callable(plugins_close):
                    plugins_close()
            except Exception:
                pass
            try:
                await db.close()
            except Exception as exc:
                failures.append(f"database close failed: {exc}")
            # sim-only cleanups: this run's plugin modules and temp key
            _purge_plugin_modules(sim_plugin_modules)
            shutil.rmtree(key_dir, ignore_errors=True)

        # publish AFTER teardown: the finally block above can still append
        # failures — the event must agree with the returned report
        try:
            await bus.publish(
                ScenarioFinished(
                    account="", name=name,
                    passed=not failures,
                    duration_s=round(time.time() - run_start, 3),
                )
            )
        except Exception:
            pass  # event publish must never fail a finished run

        duration = round(time.time() - run_start, 3)
        verdict = "passed" if not failures else "failed"
        # CAP (audit round 3): bound the dashboard payload — keep the most
        # recent lines (failure evidence lives at the tail) and announce
        # the truncation in the same spec-§9 line shape
        if len(log_lines) > _LOG_LINES_CAP:
            dropped = len(log_lines) - _LOG_LINES_CAP
            log_lines = log_lines[-_LOG_LINES_CAP:]
            log_lines.insert(
                0,
                f"0000.000s INFO    sim "
                f"log output capped at {_LOG_LINES_CAP} line(s) "
                f"- {dropped} earlier line(s) dropped",
            )
        return {
            "name": name,
            "passed": not failures,
            "duration_s": duration,
            "failures": failures,
            "log_lines": log_lines,
            "interactions": [dict(entry) for entry in interactions],
            "summary": (
                f"scenario {name!r} {verdict} in {duration:.2f}s - "
                f"{len(interactions)} interaction(s), {len(failures)} failure(s)"
            ),
        }

    # ------------------------------------------------------------- internals
    @staticmethod
    def _build_account_manager(
        mods: SimpleNamespace,
        bus: EventBus,
        logger: LogService,
        db: Any,
        plugins_mgr: Any,
        profiles: Any,
        scheduler: Any,
        settings: SettingsManager,
        audit: Any,
        client_factory: Callable[[str, str], SimClient],
        credentials: Credentials,
        limiter: Any,
        *,
        credential_service: Any = None,
        cooldowns: Any = None,
        plugin_state: Any = None,
        lifecycle: Any = None,
    ) -> Any:
        """AccountManager with the pinned positional arguments.

        ``limiter`` (and the shared services) are passed as keywords when
        the constructor accepts them; a TypeError falls back to the leaner
        construction — both variants are supported (documented seam, see
        module docstring). ``lifecycle`` (round-4: a real
        LifecycleGovernor, production parity) rides the PRIMARY
        construction only — duck-typed fakes that predate the kwarg keep
        working through the fallbacks.
        """
        try:
            return mods.AccountManager(
                bus, logger, db.accounts, plugins_mgr, profiles, scheduler,
                settings, audit, client_factory, credentials, limiter=limiter,
                api_cache_repo=db.cache,
                credential_service=credential_service, cooldowns=cooldowns,
                plugin_state=plugin_state, lifecycle=lifecycle,
            )
        except TypeError:
            try:
                return mods.AccountManager(
                    bus, logger, db.accounts, plugins_mgr, profiles, scheduler,
                    settings, audit, client_factory, credentials, limiter=limiter,
                    api_cache_repo=db.cache,
                )
            except TypeError:
                return mods.AccountManager(
                    bus, logger, db.accounts, plugins_mgr, profiles, scheduler,
                    settings, audit, client_factory, credentials,
                )


__all__ = ["SimulationRunner"]
