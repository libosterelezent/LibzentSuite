"""SimulationRunner — a COMPLETE ephemeral stack per scenario run (spec §16).

Wiring follows the pinned constructors in INTEGRATION.md exactly:

    Database(":memory:") + migrate()
    EventBus()
    LogService()                                   (no db — sim logs stay in RAM)
    SettingsManager(bus, logger, plugin_repo=db.plugins,
                    account_settings_repo=db.account_settings,
                    global_layer=DEMO_PLUGIN_SETTINGS)
    RateLimitManager(bus, logger, db.policies)
    AuditRecorder(db.audit)
    TaskScheduler(bus, logger, limiter, sender)    <- LATE-BOUND SENDER (below)
    PluginManager(bus, logger, settings, db.plugins, plugin_dir) + discover()
    ProfileManager(db.profiles, logger, db.accounts)
    AccountManager(bus, logger, db.accounts, plugins_mgr, profiles, scheduler,
                   settings, audit, client_factory, credentials[, limiter])

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

Documented contract decisions (ambiguous seams — see INTEGRATION.md ground
rules; the lead reconciles at integration):
  * LOGGER SHAPE: the spine SettingsManager and the A3/A6 managers call the
    injected logger in STDLIB style (``logger.info("fmt %s", arg)``,
    ``logger.exception(...)``), while A4's rate-limit engine calls it in
    LogService style (``logger.info(source, message)``) and A5's scheduler
    auto-detects. This runner therefore injects :class:`ServiceLogger` — a
    tiny stdlib-logger-shaped adapter over ``LogService.record`` — into every
    manager. Both call styles produce a readable structured entry (so
    ``log_lines`` and the dashboard Logs page keep working), and a logging
    failure can never break a manager.
  * ACCOUNT MANAGER ``limiter`` KEYWORD: passed when accepted (A7 assignment
    wording); the INTEGRATION.md pinned signature omits it, so a TypeError
    falls back to the keyword-less construction — either variant works.
"""
from __future__ import annotations

import logging
import sys
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
        from core.audit import AuditRecorder
        from core.plugin_manager import PluginManager
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
        AuditRecorder=AuditRecorder,
        PluginManager=PluginManager,
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
            db.migrate()
        except Exception as exc:
            raise SimulationError(
                f"scenario {name!r}: in-memory database failed: {exc}"
            ) from exc
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
        limiter = mods.RateLimitManager(bus, log_ratelimit, db.policies)
        audit = mods.AuditRecorder(db.audit)

        # LATE-BOUND SENDER SEAM — see module docstring
        box: Dict[str, Callable[[Any], Any]] = {}

        async def sender(task: Any) -> Any:
            return await box["send"](task)

        scheduler = mods.TaskScheduler(
            bus, log_scheduler, limiter, sender,
            audit=audit, history_repo=db.tasks,
        )

        plugins_mgr = mods.PluginManager(
            bus, log_plugins, settings, db.plugins, self._plugin_dir
        )
        try:
            plugins_mgr.discover()
        except Exception as exc:
            db.close()
            raise SimulationError(
                f"scenario {name!r}: plugin discovery failed in "
                f"{self._plugin_dir!r}: {exc}"
            ) from exc

        profiles = mods.ProfileManager(db.profiles, log_profiles, db.accounts)

        user_ids = {ACCOUNT_NAME: SYNTHETIC_ALPHA_ID}

        def client_factory(account_name: str, token: str) -> SimClient:
            return SimClient(world, user_ids[account_name], seed=SYNTHETIC_ALPHA_ID)

        credentials = self._credentials or Credentials.from_values(
            {
                "MUDAE_BOT_ID": str(SYNTHETIC_MUDAE_ID),
                "OWNER_USER_IDS": str(SYNTHETIC_OWNER_ID),
                f"DISCORD_TOKEN_{ACCOUNT_NAME.upper()}": f"sim-token-{ACCOUNT_NAME}",
            }
        )

        accounts = self._build_account_manager(
            mods, bus, log_accounts, db, plugins_mgr, profiles, scheduler,
            settings, audit, client_factory, credentials, limiter,
        )
        box["send"] = accounts.send_hook  # fill the seam BEFORE start()

        # a real ControlAPI + the mention-prefix bridge, so scenarios can
        # exercise Discord-side management exactly like production
        from core.api import ControlAPI

        api = ControlAPI(
            accounts=accounts, plugins=plugins_mgr, profiles=profiles,
            settings=settings, limiter=limiter, scheduler=scheduler,
            logs=logger, audit=audit, credentials=credentials,
        )
        from core.remote_control import RemoteControl

        remote = RemoteControl(
            bus, scheduler, accounts, lambda: api, credentials,
            ServiceLogger(logger, "remote-control"),
        ).start()

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
                accounts.add_account(ACCOUNT_NAME)
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
                )
                try:
                    await scenario.script(facade)
                    # one extra quiet window for stragglers after the script
                    await world.wait_for_quiet(
                        SYNTHETIC_GUILD_CHANNEL, quiet=quiet_window(), limit=90.0
                    )
                    failures.extend(scenario.checks(list(interactions), world))
                except Exception as exc:  # a scenario crash is a failed report
                    failures.append(f"scenario crashed: {type(exc).__name__}: {exc}")

            try:
                if bridge_attached_here:
                    detach_stdlib_bridge()  # never leak a bridge to a dead service
                remote.stop()
                await bus.publish(
                    ScenarioFinished(
                        account="", name=name,
                        passed=not failures,
                        duration_s=round(time.time() - run_start, 3),
                    )
                )
            except Exception:
                pass  # event publish must never fail a finished run
        finally:
            if account_started:
                try:
                    await accounts.stop(ACCOUNT_NAME)
                except Exception as exc:
                    failures.append(f"account stop failed: {exc}")
            if scheduler_started:
                try:
                    await scheduler.stop(drain=True)
                except Exception as exc:
                    failures.append(f"scheduler stop failed: {exc}")
            try:
                db.close()
            except Exception as exc:
                failures.append(f"database close failed: {exc}")
            unsub_audit()
            unsub_log()

        duration = round(time.time() - run_start, 3)
        verdict = "passed" if not failures else "failed"
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
    ) -> Any:
        """AccountManager with the pinned positional arguments.

        ``limiter`` is passed as a keyword when the constructor accepts it
        (A7 assignment wording); the INTEGRATION.md pinned signature omits
        it, so a TypeError falls back to the keyword-less construction —
        both variants are supported (documented seam, see module docstring).
        """
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
