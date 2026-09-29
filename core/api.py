"""Control API: one shared command surface used by BOTH the terminal and GUI.

Everything here is async and returns JSON-safe structures so the dashboard can
serialize responses directly and the CLI can pretty-print them.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .container import AccountContainer, ContainerError
from .credentials import Credentials
from .plugin_loader import PluginLoader
from .rate_limiter import RateLimiterService, RateRuleError
from .settings import SettingsStore


class ControlAPI:
    """High-level operations over the container + limiter."""

    def __init__(
        self,
        container: AccountContainer,
        limiter: RateLimiterService,
        loader: PluginLoader,
        credentials: Credentials,
        settings_store: Optional[SettingsStore] = None,
        state: Optional[Any] = None,
    ) -> None:
        self._container = container
        self._limiter = limiter
        self._loader = loader
        self._credentials = credentials
        self._settings_store = settings_store
        #: core.persistence.StateStore — records runtime limit changes
        self._state = state

    # ---------------------------------------------------------------- status
    async def status(self) -> Dict[str, Any]:
        """Full snapshot: accounts + limits (for dashboards and 'status')."""
        return {"accounts": self._container.status(), "limits": self._limiter.stats()}

    async def limits(self) -> Dict[str, Any]:
        """Rate limiter stats only."""
        return self._limiter.stats()

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
        await self._container.enable_plugin(account, plugin, enabled)
        return {"account": account, "plugin": plugin, "enabled": enabled}

    # ---------------------------------------------------------------- limits
    async def set_limit(
        self, action: str, rule: str, account: Optional[str] = None
    ) -> Dict[str, Any]:
        """Set a rate rule for ALL accounts (account=None) or one bot.

        Raises RateRuleError on malformed rule strings.
        """
        if account is None:
            parsed = self._limiter.set_default_rule(action, rule)
            return {"action": action, "account": None, "rule": str(parsed)}
        parsed = self._limiter.set_account_rule(account, action, rule)
        if self._state is not None:
            self._state.set_limit_override(account, action, str(parsed))
        return {"action": action, "rule": str(parsed), "account": account}

    async def clear_limit(self, account: str, action: str) -> Dict[str, Any]:
        """Remove a per-bot override so it inherits the global rule again."""
        removed = self._limiter.clear_account_rule(account, action)
        if removed and self._state is not None:
            self._state.clear_limit_override(account, action)
        return {"account": account, "action": action, "cleared": removed}
