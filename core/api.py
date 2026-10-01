"""CoreAPI — the ONE application surface both interfaces call (spec §13/§14).

CLI and dashboard are thin wrappers over these methods and NOTHING else, so
they can never drift apart in capability (parity by construction). This
facade is intentionally duck-typed over the managers: it stores whatever it
is given and delegates. Managers are constructed by core/bootstrap.py.

Method map (grouped like the dashboard navigation, spec §12):
  status            overview: accounts + limits + tasks + warnings
  accounts_*        list/add/remove/info/enable/start/stop/emergency-stop
  plugins_*         manifests/state/enable/settings/set-setting
  profiles_*        list/create/delete/assign/unassign
  ratelimit_*       policies/set/clear/effective/stats/warnings/set-floor
  tasks_*           list/cancel/cancel-matching/history
  simulation_*      list/run
  logs_tail / audit_tail / credentials_summary
  emergency_stop_all / shutdown
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # duck-typed at runtime — no manager imports here
    from .account_manager import AccountManager
    from .audit import AuditRecorder
    from .credentials import Credentials
    from .logging_service import LogService
    from .plugin_manager import PluginManager
    from .profile_manager import ProfileManager
    from .rate_limit_manager import RateLimitManager
    from .settings_manager import SettingsManager
    from .task_scheduler import TaskScheduler


def parse_bool(value: Any, field: str) -> bool:
    """Strict boolean parsing shared by every mutation surface.

    bool("false") == True silently did the OPPOSITE of the request once —
    JSON/CLI surfaces must carry real booleans; anything else is rejected.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"field '{field}' must be a boolean (true/false), got {value!r}")


class ControlAPI:
    """High-level operations over every manager (JSON-safe in/out)."""

    def __init__(
        self,
        *,
        accounts: "AccountManager",
        plugins: "PluginManager",
        profiles: "ProfileManager",
        settings: "SettingsManager",
        limiter: "RateLimitManager",
        scheduler: "TaskScheduler",
        logs: "LogService",
        audit: Optional["AuditRecorder"] = None,
        simulation: Any = None,          # adapters.simulator.runner.SimulationRunner
        credentials: Optional["Credentials"] = None,
        database: Any = None,
        started_at: float = 0.0,
    ) -> None:
        self._accounts = accounts
        self._plugins = plugins
        self._profiles = profiles
        self._settings = settings
        self._limiter = limiter
        self._scheduler = scheduler
        self._logs = logs
        self._audit = audit
        self._simulation = simulation
        self._credentials = credentials
        self._database = database
        self._started_at = started_at
        self._booted_at = __import__("time").time()

    # ---------------------------------------------------------------- status
    async def status(self) -> Dict[str, Any]:
        """Overview snapshot: accounts + rate limits + tasks + warnings."""
        import time as _time

        return {
            "accounts": await self._accounts.accounts(),
            "ratelimit": self._limiter.stats(),
            "tasks": self._scheduler.stats(),
            "warnings": self._limiter.warnings(),
            "uptime_s": round(_time.time() - self._booted_at, 1),
        }

    # -------------------------------------------------------------- accounts
    async def accounts_list(self) -> List[Dict[str, Any]]:
        """Every account: name, discord id (when running), status, profiles."""
        return await self._accounts.accounts()

    async def accounts_add(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Register + persist an account. Token VALUE stays in .env.

        payload: {name (required), token_ref?, sync_group?, enabled?,
                  profiles?: [names]}. Unknown keys are rejected (typo trap).
        """
        if not isinstance(payload, dict) or not payload.get("name"):
            raise ValueError("account payload needs at least {'name': ...}")
        allowed = {"name", "token_ref", "sync_group", "enabled", "profiles"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown account payload keys: {sorted(unknown)}")
        profiles = payload.get("profiles") or []
        if not isinstance(profiles, list) or not all(isinstance(p, str) for p in profiles):
            raise ValueError(f"'profiles' must be a list of strings, got {profiles!r}")
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("field 'enabled' must be a boolean")
        return self._accounts.add_account(
            payload["name"],
            token_ref=payload.get("token_ref"),
            enabled=enabled,
            sync_group=payload.get("sync_group"),
            profiles=profiles or None,
        )

    async def accounts_remove(self, name: str) -> Dict[str, Any]:
        """Remove a STOPPED account from runtime + database (cascade)."""
        return self._accounts.remove_account(name)

    async def accounts_info(self, name: str) -> Dict[str, Any]:
        """Account details page (spec §8): status/identity/runtime/plugins/rate."""
        return self._accounts.details(name)

    async def accounts_enable(self, name: str, enabled: bool) -> Dict[str, Any]:
        return self._accounts.set_enabled(name, parse_bool(enabled, "enabled"))

    async def accounts_start(self, name: Optional[str] = None) -> Dict[str, Any]:
        """Start one account (None = every enabled account)."""
        return await self._accounts.start(name) if name else await self._accounts.start_all()

    async def accounts_stop(self, name: Optional[str] = None) -> Dict[str, Any]:
        """Stop one account (None = all) — graceful."""
        return await self._accounts.stop(name) if name else await self._accounts.stop_all()

    async def accounts_emergency_stop(self, name: str) -> Dict[str, Any]:
        """Account-scoped emergency stop: cancel its tasks NOW + disconnect."""
        return self._accounts.emergency_stop(name)

    # --------------------------------------------------------------- plugins
    async def plugins_manifests(self) -> List[Dict[str, Any]]:
        """Installed plugins incl. their settings schema (auto-generated UIs)."""
        return self._plugins.manifests()

    async def plugins_state(self, account: str) -> List[Dict[str, Any]]:
        """Per-account plugin runtime state incl. resolved settings + errors."""
        return self._plugins.states(account)

    async def plugins_set_enabled(
        self, account: str, plugin: str, enabled: bool
    ) -> Dict[str, Any]:
        """Enable/disable a plugin on an account (lifecycle hooks fire)."""
        await self._plugins.set_enabled(account, plugin, parse_bool(enabled, "enabled"))
        return {"account": account, "plugin": plugin, "enabled": enabled}

    async def plugins_settings(self, account: str) -> List[Dict[str, Any]]:
        """Resolved settings per plugin for one account."""
        return [
            {"name": state["name"], "settings": state["settings"]}
            for state in self._plugins.states(account)
        ]

    async def plugins_set_setting(
        self, account: str, plugin: str, key: str, value: Any
    ) -> Dict[str, Any]:
        """Change ONE plugin setting for ONE account (validated + persisted)."""
        out = self._plugins.set_setting(account, plugin, key, value)
        return {"account": account, "plugin": plugin, "key": key, "value": out}

    # -------------------------------------------------------------- profiles
    async def profiles_list(self) -> List[Dict[str, Any]]:
        return self._profiles.profiles()

    async def profiles_create(
        self, name: str, plugins: List[str], description: str = ""
    ) -> Dict[str, Any]:
        if not isinstance(plugins, list) or not all(isinstance(p, str) for p in plugins):
            raise ValueError("'plugins' must be a list of plugin names")
        return self._profiles.set_profile(name, plugins, description)

    async def profiles_delete(self, name: str) -> Dict[str, Any]:
        return self._profiles.delete_profile(name)

    async def profiles_assign(self, account: str, profile: str) -> Dict[str, Any]:
        """Grant a profile (applies on the account's next start)."""
        return self._profiles.assign(account, profile)

    async def profiles_unassign(self, account: str, profile: str) -> Dict[str, Any]:
        return self._profiles.unassign(account, profile)

    # ------------------------------------------------------------- rate limits
    async def ratelimit_policies(self) -> List[Dict[str, Any]]:
        """Every stored policy with its level/owner scope."""
        return self._limiter.policies()

    async def ratelimit_set(
        self,
        action: str,
        interval: float,
        jitter_min: float = 0.0,
        jitter_max: float = 0.0,
        level: str = "global",
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Set a policy. Enforces the safety floor: < 1.5s/action is REJECTED."""
        policy = self._limiter.set_policy_values(
            action, interval, jitter_min, jitter_max, level, owner
        )
        return {"action": action, "level": level, "owner": owner, "policy": str(policy)}

    async def ratelimit_clear(
        self, level: str, owner: Optional[str], action: str
    ) -> Dict[str, Any]:
        removed = self._limiter.clear_policy(level, owner, action)
        return {"cleared": removed, "level": level, "owner": owner, "action": action}

    async def ratelimit_effective(
        self, action: str, account: Optional[str] = None, plugin: Optional[str] = None
    ) -> Dict[str, Any]:
        """What actually paces this (account, plugin, action) right now."""
        return self._limiter.effective(action, account, plugin)

    async def ratelimit_stats(self) -> Dict[str, Any]:
        """Visualization data: policies, cooldowns, last fires, counters."""
        return self._limiter.stats()

    async def ratelimit_warnings(self) -> List[Dict[str, Any]]:
        """Safety review of the current configuration."""
        return self._limiter.warnings()

    async def ratelimit_set_floor(self, seconds: float) -> Dict[str, Any]:
        """Raise the pacing floor (can never drop below 1.5s)."""
        return {"floor": self._limiter.set_floor(seconds)}

    # ------------------------------------------------------------------ tasks
    async def tasks_list(
        self, status: Optional[str] = None, account: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Queued/running tasks (and recent ones) with optional filters."""
        return self._scheduler.tasks(status=status, account=account)

    async def tasks_cancel(self, task_id: int) -> Dict[str, Any]:
        return {"cancelled": self._scheduler.cancel(int(task_id)), "task_id": task_id}

    async def tasks_cancel_matching(
        self, account: Optional[str] = None, plugin: Optional[str] = None
    ) -> Dict[str, Any]:
        """Emergency-stop helper: cancel every matching task."""
        return {"cancelled": self._scheduler.cancel_matching(account=account, plugin=plugin)}

    async def tasks_history(self, limit: int = 50, account: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._scheduler.history(limit=limit, account=account)

    # ------------------------------------------------------------- simulation
    async def simulation_list(self) -> List[Dict[str, Any]]:
        if self._simulation is None:
            return []
        return self._simulation.list()

    async def simulation_run(self, name: str) -> Dict[str, Any]:
        """Run one scenario offline; returns the full timed report."""
        if self._simulation is None:
            raise RuntimeError("simulation runner not configured")
        return await self._simulation.run(name)

    # ------------------------------------------------------------------- logs
    async def logs_tail(
        self, n: int = 100, account: Optional[str] = None, severity: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Structured log tail (dashboard Logs page + CLI 'logs')."""
        return self._logs.tail(n, account=account, severity=severity)

    def logs_service(self) -> "LogService":
        """The live service (dashboard WS subscribes to this)."""
        return self._logs

    def audit_recorder(self) -> Optional["AuditRecorder"]:
        """The live audit recorder (dashboard WS subscription point)."""
        return self._audit

    async def audit_tail(
        self, n: int = 100, account: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if self._audit is None:
            return []
        return self._audit.history(limit=n, account=account)

    # ---------------------------------------------------------------- system
    def credentials_summary(self) -> Dict[str, str]:
        """Redacted credential view (tokens masked; ids shown)."""
        return self._credentials.redacted() if self._credentials else {}

    @property
    def dashboard_token(self) -> Optional[str]:
        return self._credentials.dashboard_token if self._credentials else None

    async def emergency_stop_all(self) -> Dict[str, Any]:
        """GLOBAL emergency stop: cancel all tasks, stop all accounts, halt."""
        cancelled = self._scheduler.cancel_matching()
        await self._scheduler.stop(drain=False)
        stopped = await self._accounts.stop_all()
        self._logs.error("system", f"GLOBAL EMERGENCY STOP executed ({cancelled} task(s) cancelled)")
        return {"cancelled_tasks": cancelled, "accounts": stopped, "halted": True}

    async def shutdown(self) -> Dict[str, Any]:
        """Graceful shutdown of everything (used by quit/dashboard shutdown)."""
        await self._scheduler.stop(drain=True)
        await self._accounts.stop_all()
        if self._database is not None and hasattr(self._database, "close"):
            self._database.close()
        return {"shutdown": True}


__all__ = ["ControlAPI", "parse_bool"]
