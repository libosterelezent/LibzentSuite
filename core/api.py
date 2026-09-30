"""Control API: one shared command surface used by BOTH the terminal and GUI.

Everything here is async, JSON-safe, persisted to the database when it
changes runtime state, and guarded by the selfbot safety floor — so the CLI
and the dashboard can never drift apart in capability (settings parity).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .account import AccountConfig, AccountStatus
from .container import AccountContainer, ContainerError
from .credentials import Credentials
from .plugin_loader import PluginLoader
from .rate_limiter import RateLimiterService, RateRule, RateRuleError
from .settings import SettingValidationError, SettingsStore
from .warnings import (
    UnsafeRateRuleError,
    enforce_floor,
    evaluate_limiter,
    format_warnings,
)

LOG = logging.getLogger("slaughtermudae.api")


def _as_bool(value: Any, field: str) -> bool:
    """Strict boolean parsing shared by every mutation surface.

    ERROR AUDIT FIX: bool("false") == True silently did the OPPOSITE of the
    user's request. JSON surfaces must send real booleans — strings are
    rejected so nothing is ever guessed.
    """
    if isinstance(value, bool):
        return value
    raise ValueError(f"field '{field}' must be a boolean (true/false), got {value!r}")


class ControlAPI:
    """High-level operations over the container + limiter + database."""

    def __init__(
        self,
        container: AccountContainer,
        limiter: RateLimiterService,
        loader: PluginLoader,
        credentials: Credentials,
        settings_store: Optional[SettingsStore] = None,
        state: Optional[Any] = None,
        roles: Optional[Any] = None,
        audit: Optional[Any] = None,
    ) -> None:
        self._container = container
        self._limiter = limiter
        self._loader = loader
        self._credentials = credentials
        self._settings_store = settings_store
        #: core.database.Database
        self._state = state
        #: core.roles.RoleRegistry
        self._roles = roles
        #: core.audit.AuditRecorder
        self._audit = audit

    # ---------------------------------------------------------------- status
    async def status(self) -> Dict[str, Any]:
        """Full snapshot: accounts (names + ids) + limits + safety warnings."""
        return {
            "accounts": self._container.status(),
            "limits": self._limiter.stats(),
            "warnings": [w.as_dict() for w in evaluate_limiter(self._limiter)],
        }

    async def limits(self) -> Dict[str, Any]:
        """Rate limiter stats only (rules, buckets, delays, queues)."""
        return self._limiter.stats()

    async def warnings(self) -> List[Dict[str, Any]]:
        """Current safety warnings (aggressive rules, missing delays...)."""
        return [w.as_dict() for w in evaluate_limiter(self._limiter)]

    async def plugins(self) -> list:
        """Discovered plugin manifests (metadata + settings they support)."""
        return self._loader.manifests()

    async def account_plugins(self, account: str) -> list:
        """Per-account plugin state incl. resolved settings."""
        runtime = self._container._get(account)  # noqa: SLF001 - deliberate facade
        return [
            {"name": name, "enabled": prt.enabled, "settings": prt.settings}
            for name, prt in sorted(runtime.plugins.items())
        ]

    def credentials_summary(self) -> Dict[str, str]:
        """Redacted credential view (tokens masked; ids shown)."""
        return self._credentials.redacted()

    @property
    def dashboard_token(self) -> Optional[str]:
        """Optional auth token the dashboard should require (from .env)."""
        return self._credentials.dashboard_token

    # -------------------------------------------------------------- accounts
    async def add_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Register + persist a new account (token VALUE stays in .env).

        payload: {name (required), token_ref?, sync_group?, roles?, plugins?,
                  settings?, rate_rules?, reconnect?}. ERROR AUDIT FIX:
        unknown keys are rejected (typos were silently dropped) and roles
        must be a list (a string was char-split into phantom roles); a DB
        failure after registration rolls the runtime entry back.
        """
        if not isinstance(payload, dict) or not payload.get("name"):
            raise ContainerError("account payload needs at least {'name': ...}")
        allowed = {"name", "token_ref", "sync_group", "roles", "plugins", "settings", "rate_rules", "reconnect"}
        unknown = set(payload) - allowed
        if unknown:
            raise ContainerError(f"unknown account payload keys: {sorted(unknown)}")
        roles = payload.get("roles") or []
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            raise ContainerError(f"'roles' must be a list of strings, got {roles!r}")
        config = AccountConfig(
            name=payload["name"],
            token_ref=payload.get("token_ref"),
            sync_group=payload.get("sync_group"),
            roles=list(roles),
            plugins=payload.get("plugins"),
            settings=payload.get("settings") or {},
            rate_rules=payload.get("rate_rules") or {},
            reconnect=payload.get("reconnect"),
        )
        self._container.add_account(config)  # validates name/settings (fail fast)
        if self._state is not None:
            try:
                self._state.upsert_account(config)
            except Exception:
                # ERROR AUDIT FIX: no orphan — runtime without DB row
                self._container.remove_account(config.name)
                raise
        return {"added": config.name, "roles": config.roles, "sync_group": config.sync_group}

    async def remove_account(self, name: str) -> Dict[str, Any]:
        """Remove an account (must be stopped) from runtime + database.

        Unknown accounts raise ContainerError. ERROR AUDIT FIX: the DB row is
        deleted FIRST (a later runtime failure can no longer resurrect the
        account on the next boot), and running accounts are explicitly
        reported as stopped in the result.
        """
        runtime = self._container._get(name)  # unknown accounts raise here
        was_running = (
            runtime.session is not None and runtime.session.status.value == "running"
        )
        await self._container.stop_account(name)  # no-op when already stopped
        if self._state is not None:
            self._state.delete_account(name)
        self._container.remove_account(name)
        message = "stopped and removed" if was_running else "removed"
        return {"removed": name, "was_running": was_running, "message": message}

    async def account_details(self, name: str) -> Dict[str, Any]:
        """Deep view of one account: status, roles, cached Discord data.

        The 'cached' section comes EXCLUSIVELY from the client's in-memory
        state (user, guilds, channels, DMs) — no extra Discord API calls.
        """
        runtime = self._container._get(name)  # noqa: SLF001
        snapshot: Dict[str, Any] = {"available": False}
        session = runtime.session
        if session is not None:
            try:
                snapshot = session.cached_snapshot()
            except Exception as exc:  # never break the details page
                snapshot = {"available": False, "error": str(exc)}
        return {
            "name": name,
            "status": session.status.value if session else AccountStatus.CREATED.value,
            "user_id": session.user_id if session else None,
            "last_error": session.last_error if session else None,
            "sync_group": runtime.config.sync_group,
            "roles": list(runtime.config.roles),
            "reconnect": (runtime.config.reconnect or {}),
            "cached": snapshot,
            "plugins": [
                {
                    "name": pname,
                    "enabled": prt.enabled,
                    "settings": prt.settings,
                    "last_error": prt.last_error,
                    "error_count": prt.error_count,
                }
                for pname, prt in sorted(runtime.plugins.items())
            ],
        }

    # -------------------------------------------------------------- lifecycle
    async def start(self, account: Optional[str] = None) -> Dict[str, Any]:
        """Start one account or all; returns fresh status."""
        if account is None:
            await self._container.start_all()
        else:
            await self._container.start_account(account)
        return await self.status()

    async def stop(self, account: Optional[str] = None) -> Dict[str, Any]:
        """Stop one account or all; returns fresh status."""
        if account is None:
            await self._container.stop_all()
        else:
            await self._container.stop_account(account)
        return await self.status()

    async def set_plugin_enabled(self, account: str, plugin: str, enabled: bool) -> Dict[str, Any]:
        """Enable/disable a plugin on an account at runtime."""
        enabled = _as_bool(enabled, "enabled")
        await self._container.enable_plugin(account, plugin, enabled)
        return {"account": account, "plugin": plugin, "enabled": enabled}

    # -------------------------------------------------------------- settings
    def set_plugin_setting(self, account: str, plugin: str, key: str, value: Any) -> Dict[str, Any]:
        """Change ONE plugin setting for ONE account (validated + persisted).

        Shared by CLI 'set' and the dashboard Plugin Settings page.
        """
        validated = self._container.set_plugin_setting(account, plugin, key, value)
        return {"account": account, "plugin": plugin, "key": key, "value": validated}

    # ---------------------------------------------------------------- limits
    async def set_limit(
        self, action: str, rule: str, account: Optional[str] = None
    ) -> Dict[str, Any]:
        """Set a rate rule for ALL accounts (account=None) or one bot.

        Enforces the selfbot safety floor: rules faster than 1.5s/action are
        REJECTED (UnsafeRateRuleError) — for a single bot or globally alike.
        """
        parsed = RateRule.parse(rule)  # RateRuleError on malformed input
        enforce_floor(action, parsed, account)
        if account is None:
            applied = self._limiter.set_default_rule(action, parsed)
            # AUDIT FIX: global rules persist too (kv row) — previously a
            # dashboard/CLI global change silently reverted on restart
            if self._state is not None:
                self._state.kv_set(f"global_rule:{action}", str(applied))
            return {"action": action, "account": None, "rule": str(applied)}
        applied = self._limiter.set_account_rule(account, action, parsed)
        if self._state is not None:
            self._state.set_limit_override(account, action, str(applied))
        return {"action": action, "rule": str(applied), "account": account}

    async def clear_limit(self, account: str, action: str) -> Dict[str, Any]:
        """Remove a per-bot override so it inherits the global rule again."""
        removed = self._limiter.clear_account_rule(account, action)
        if removed and self._state is not None:
            self._state.clear_limit_override(account, action)
        return {"account": account, "action": action, "cleared": removed}

    # ---------------------------------------------------------------- delays
    async def set_delay(
        self, action: str, minimum: float, maximum: float, account: Optional[str] = None
    ) -> Dict[str, Any]:
        """Human-like pause range (x.xx–y.yy s) applied after every grant.

        account=None sets the global default; per-account overrides win.
        """
        spec = self._limiter.set_delay(action, minimum, maximum, account)
        if self._state is not None:
            self._state.set_delay(action, minimum, maximum, account)
        return {"action": action, "account": account, "delay": str(spec)}

    async def clear_delay(self, action: str, account: Optional[str] = None) -> Dict[str, Any]:
        """Remove a delay range (per-account when given, else the global one)."""
        removed = self._limiter.clear_delay(action, account)
        if removed and self._state is not None:
            self._state.clear_delay(action, account)
        return {"action": action, "account": account, "cleared": removed}

    # ----------------------------------------------------------------- roles
    async def roles(self) -> List[Dict[str, Any]]:
        """All defined roles with the plugins they permit."""
        if self._roles is None:
            return []
        return self._roles.manifests()

    async def set_role(self, name: str, plugins: List[str], description: str = "") -> Dict[str, Any]:
        """Create/update a role (persisted). Takes effect on next account start."""
        if self._roles is None:
            raise ContainerError("no role registry configured")
        role = self._roles.set_role(name, plugins, description)
        return {"role": role.name, "plugins": role.plugins, "description": role.description}

    async def delete_role(self, name: str) -> Dict[str, Any]:
        """Delete a role; holders simply lose that grant."""
        if self._roles is None:
            raise ContainerError("no role registry configured")
        return {"deleted": name, "existed": self._roles.delete_role(name)}

    async def assign_role(self, account: str, role: str, assigned: bool) -> Dict[str, Any]:
        """Grant/revoke a role on an account (persisted; applies on next start).

        ERROR AUDIT FIX: unknown roles are rejected instead of silently
        persisted as a grant that allows nothing.
        """
        runtime = self._container._get(account)  # noqa: SLF001
        if self._roles is not None and assigned and self._roles.get(role) is None:
            known = sorted(self._roles.all())
            raise ContainerError(f"unknown role {role!r} (defined: {known or 'none yet'})")
        assigned = _as_bool(assigned, "assigned")
        roles = self._container.assign_role(account, role, assigned)
        if self._state is not None:
            self._state.upsert_account(runtime.config)
        return {"account": account, "roles": roles}

    # ------------------------------------------------------------- send engine
    async def set_engine(
        self, account: str, enabled: bool, prefixrate: Optional[float] = None
    ) -> Dict[str, Any]:
        """Toggle/tune the account's serialized send lane (FarmLite prefixrate).

        enabled=True attaches (or retunes) the engine — prefixrate 0 PAUSES
        the lane (tasks wait) per FarmLite semantics. Persisted in the DB so
        the policy survives restarts. Shared by CLI + dashboard.
        """
        import json as _json

        from .send_engine import SendEngine

        runtime = self._container._get(account)
        effective = prefixrate
        if effective is None:
            effective = (
                runtime.engine.prefixrate if runtime.engine is not None
                else self._container._send_policy.prefixrate
            )
        effective = float(effective)
        if effective < 0:
            raise ValueError(f"prefixrate must be >= 0, got {effective}")
        if enabled:
            if runtime.engine is None:
                runtime.engine = SendEngine(account, prefixrate=effective).start()
                if runtime.session is not None:
                    runtime.session._engine = runtime.engine
            elif prefixrate is not None:
                runtime.engine.set_prefixrate(effective)
        else:
            await self._container._shutdown_engine(runtime)
            if runtime.session is not None:
                runtime.session._engine = None
        if self._state is not None:
            self._state.kv_set(
                f"engine:{account}", _json.dumps({"enabled": enabled, "prefixrate": effective})
            )
        return runtime.engine.stats() if runtime.engine else {
            "account": account, "enabled": False, "running": False,
        }

    # ----------------------------------------------------------------- audit
    def audit(self, limit: int = 100, account: Optional[str] = None) -> List[Dict[str, Any]]:
        """Interaction history — DB-backed when available, else the ring."""
        if self._audit is None:
            return []
        return self._audit.history(limit=limit, account=account)

    def audit_recorder(self) -> Optional[Any]:
        """The live AuditRecorder (dashboard WS subscription point)."""
        return self._audit

    # ---------------------------------------------------------------- helpers
    def format_warnings_text(self) -> List[str]:
        """Warnings as display lines (CLI/TUI share this)."""
        return format_warnings(evaluate_limiter(self._limiter))
