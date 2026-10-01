"""PluginManager: per-account plugin instances, lifecycle, dispatch (spec §2).

One manager owns every (account, plugin) pair in the fleet:

  * ``attach_account``   instantiate instances for an account (profile-filtered),
  * ``activate_account`` resolve settings + fire on_load/on_enable (async —
                         lifecycle ordering is awaited, not fire-and-forget),
  * ``deactivate_account`` fire on_disable/on_unload (async, error-isolated),
  * ``set_enabled``      persist-first runtime toggling with lifecycle hooks,
  * ``set_setting``      validated setting changes with a live settings view,
  * ``states``           per-plugin runtime state incl. error counters.

CONTRACT NOTE (deviation, documented): INTEGRATION.md sketched
activate/deactivate as sync, but plugin lifecycle hooks are async — awaiting
them requires async methods. They are async here; the account manager calls
them from its own async start/stop flows.

Error isolation is absolute: a failing hook or handler disables/records THAT
plugin only — the ported discipline from three audit rounds.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from core.errors import PluginError
from core.event_bus import (
    EventBus,
    Event,
    MessageReceived,
    PluginEnabledChanged,
    PluginLoaded,
    PluginUnloaded,
)
from core.settings_manager import SettingsManager

from .loader import PluginLoader


@dataclass
class PluginRuntime:
    """One plugin instance bound to one account (manager-internal)."""

    name: str
    instance: Any                      # PluginBase (duck-typed: has hooks)
    enabled: bool = True
    loaded: bool = False
    active: bool = False               # activate_account ran (services wired)
    settings: Dict[str, Any] = field(default_factory=dict)
    last_error: Optional[str] = None
    error_count: int = 0


class PluginManager:
    """Discovery + lifecycle + event dispatch for every account's plugins."""

    def __init__(
        self,
        bus: EventBus,
        logger: logging.Logger,
        settings: SettingsManager,
        plugin_repo: Any = None,
        plugin_dir: str = "plugins",
    ) -> None:
        self._bus = bus
        self._logger = logger
        self._settings = settings
        self._repo = plugin_repo            # database PluginRepository (optional)
        self._loader = PluginLoader(plugin_dir)
        #: account -> plugin name -> PluginRuntime
        self._accounts: Dict[str, Dict[str, PluginRuntime]] = {}
        #: account -> services factory: (plugin_name) -> PluginServices
        self._services: Dict[str, Callable[[str], Any]] = {}
        #: strong refs for fire-and-forget dispatch tasks (no GC mid-flight)
        self._pending: Set[asyncio.Task] = set()
        self._unsubscribe = bus.subscribe(MessageReceived, self._on_message)
        self._discovered = False

    # ------------------------------------------------------------- discovery
    def discover(self) -> Dict[str, Any]:
        """Scan the plugin dir (raises PluginError on inconsistency).

        Also refreshes plugin metadata rows in the repository when present —
        the Plugins page reads them even before any account attaches.
        """
        records = self._loader.discover()
        self._discovered = True
        if self._repo is not None:
            for name, rec in records.items():
                try:
                    self._repo.upsert_metadata(
                        name,
                        rec.cls.version,
                        rec.cls.description,
                        [s.as_manifest() for s in rec.cls.settings],
                    )
                except Exception:
                    self._logger.exception("plugin metadata persist failed for %s", name)
        return records

    def _ensure_discovered(self) -> None:
        if not self._discovered:
            self.discover()

    def manifests(self) -> List[Dict[str, Any]]:
        """JSON-safe manifests (metadata + settings schema) for both UIs."""
        self._ensure_discovered()
        return self._loader.manifests()

    def names(self) -> List[str]:
        self._ensure_discovered()
        return self._loader.names()

    def _schema_for(self, plugin: str) -> tuple:
        """Setting schema of a plugin: from the loader record."""
        self._ensure_discovered()
        record = self._loader.record(plugin)  # PluginError when unknown
        return tuple(record.cls.settings)

    # -------------------------------------------------------------- accounts
    def attach_account(self, account: str, allowed: Optional[Set[str]] = None) -> None:
        """Instantiate this account's plugins (profile allowlist applies).

        ``allowed=None`` keeps every discovered plugin; otherwise only names in
        the set survive (union of the account's profiles, computed by the
        account manager). Settings resolve later, at activate time.
        """
        self._ensure_discovered()
        wanted = [n for n in self._loader.names() if allowed is None or n in allowed]
        runtimes: Dict[str, PluginRuntime] = {}
        for name in wanted:
            enabled = True
            if self._repo is not None:
                try:
                    enabled = self._repo.state(account, name)
                    if enabled is None:
                        enabled = True
                except Exception:
                    self._logger.exception("plugin state read failed for %s:%s", account, name)
            runtimes[name] = PluginRuntime(
                name=name, instance=self._loader.create(name), enabled=bool(enabled)
            )
        # resolve settings NOW (not just at activate) so bad config fails
        # fast at attach — ported behavior from the audited original
        for runtime in runtimes.values():
            try:
                runtime.settings = self._settings.resolve(
                    runtime.name, self._schema_for(runtime.name), account=account
                )
            except Exception as exc:
                runtime.enabled = False
                runtime.last_error = f"settings: {type(exc).__name__}: {exc}"
                runtime.error_count += 1
                self._logger.error(
                    "plugin %s on %s failed settings resolution; disabled (%s)",
                    runtime.name, account, exc,
                )
        self._accounts[account] = runtimes
        self._logger.info(
            "attached %d plugin(s) to %s (allowed=%s)",
            len(runtimes), account, sorted(allowed) if allowed is not None else "all",
        )

    def detach_account(self, account: str) -> None:
        """Drop the account's instances WITHOUT firing unload hooks.

        The account manager calls deactivate_account first when the plugins
        were active; detach only clears bookkeeping.
        """
        self._accounts.pop(account, None)
        self._services.pop(account, None)

    async def activate_account(self, account: str, services: Any) -> None:
        """Resolve settings + fire on_load/on_enable for enabled plugins.

        ``services`` is EITHER a PluginServices-like object OR a factory
        ``(plugin_name) -> services`` (duck-checked) — each plugin gets its
        own facade either way.
        """
        runtimes = self._accounts.get(account)
        if runtimes is None:
            raise PluginError(f"account {account!r} has no attached plugins (attach first)")
        factory = self._services_factory(services)
        self._services[account] = factory
        for runtime in runtimes.values():
            if not runtime.enabled:
                continue
            await self._activate_one(account, runtime)
        self._logger.info("activated plugins on %s", account)

    async def _activate_one(self, account: str, runtime: PluginRuntime) -> None:
        """Load + enable one runtime, isolating any hook failure."""
        services = self._services.get(account, lambda _n: None)(runtime.name)
        try:
            runtime.settings = self._settings.resolve(
                runtime.name, self._schema_for(runtime.name), account=account
            )
        except Exception as exc:
            runtime.enabled = False
            runtime.last_error = f"settings: {type(exc).__name__}: {exc}"
            runtime.error_count += 1
            self._logger.error(
                "plugin %s on %s failed settings resolution; disabled (%s)",
                runtime.name, account, exc,
            )
            return
        ctx = self._context(account, runtime, services)
        try:
            if not runtime.loaded:
                await self._hook(account, runtime, runtime.instance.on_load, ctx, "on_load")
                runtime.loaded = True
            await self._hook(account, runtime, runtime.instance.on_enable, ctx, "on_enable")
            runtime.active = True
            self._bus.publish_nowait(PluginLoaded(account=account, plugin=runtime.name))
        except Exception:
            runtime.enabled = False  # _hook already recorded the error
            self._logger.error(
                "plugin %s failed to activate on %s; disabled", runtime.name, account
            )

    async def deactivate_account(self, account: str) -> None:
        """Fire on_disable + on_unload for the account's active plugins."""
        runtimes = self._accounts.get(account)
        if runtimes is None:
            return
        for runtime in runtimes.values():
            services = self._services.get(account, lambda _n: None)(runtime.name)
            ctx = self._context(account, runtime, services)
            if runtime.active and runtime.enabled:
                try:
                    await self._hook(account, runtime, runtime.instance.on_disable, ctx, "on_disable")
                except Exception:
                    pass  # recorded; still unload
            if runtime.loaded:
                try:
                    await self._hook(account, runtime, runtime.instance.on_unload, ctx, "on_unload")
                except Exception:
                    pass
            runtime.active = False
            runtime.loaded = False
            self._bus.publish_nowait(PluginUnloaded(account=account, plugin=runtime.name))
        self._services.pop(account, None)

    # ------------------------------------------------------------- mutation
    async def set_enabled(self, account: str, plugin: str, enabled: bool) -> None:
        """Toggle a plugin at runtime (persisted BEFORE memory mutates)."""
        runtime = self._runtime(account, plugin)
        if self._repo is not None:
            self._repo.set_state(account, plugin, bool(enabled))
        if enabled and not runtime.enabled:
            runtime.enabled = True
            if account in self._services:  # account is active: fire hooks
                await self._activate_one(account, runtime)
        elif not enabled and runtime.enabled:
            runtime.enabled = False
            if runtime.active:
                services = self._services.get(account, lambda _n: None)(plugin)
                ctx = self._context(account, runtime, services)
                try:
                    await self._hook(account, runtime, runtime.instance.on_disable, ctx, "on_disable")
                except Exception:
                    pass
        self._bus.publish_nowait(
            PluginEnabledChanged(account=account, plugin=plugin, enabled=enabled)
        )

    def set_setting(self, account: str, plugin: str, key: str, value: Any) -> Any:
        """Validate + persist + apply one plugin setting for one account."""
        runtime = self._runtime(account, plugin)
        schema = self._schema_for(plugin)
        validated = self._settings.set_plugin_setting(account, plugin, key, value, schema)
        runtime.settings[key] = validated  # live view (contexts share the dict)
        return validated

    # ---------------------------------------------------------------- state
    def states(self, account: str) -> List[Dict[str, Any]]:
        """Per-account plugin state for both UIs (sorted by name)."""
        runtimes = self._accounts.get(account, {})
        return [
            {
                "name": rt.name,
                "enabled": rt.enabled,
                "loaded": rt.loaded,
                "settings": dict(rt.settings),
                "last_error": rt.last_error,
                "error_count": rt.error_count,
            }
            for rt in sorted(runtimes.values(), key=lambda r: r.name)
        ]

    def allowed_names(self, account: str) -> List[str]:
        """Names currently attached to the account (for error messages)."""
        return sorted(self._accounts.get(account, {}))

    # -------------------------------------------------------------- routing
    def _on_message(self, event: Event) -> None:
        """Bus handler (sync): fan a MessageReceived out as a fire-and-forget
        dispatch task so the publisher is never blocked by a slow plugin."""
        if not isinstance(event, MessageReceived):
            return
        runtimes = self._accounts.get(event.account)
        if not runtimes:
            return
        task = asyncio.get_running_loop().create_task(self._dispatch(event, runtimes))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _dispatch(self, event: MessageReceived, runtimes: Dict[str, PluginRuntime]) -> None:
        """Await every enabled plugin's handle_event, isolated per plugin."""
        for runtime in runtimes.values():
            if not runtime.enabled or not runtime.active:
                continue
            services = self._services.get(event.account, lambda _n: None)(runtime.name)
            ctx = self._context(event.account, runtime, services)
            try:
                await runtime.instance.handle_event(ctx, event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                runtime.error_count += 1
                runtime.last_error = f"{type(event).__name__}: {type(exc).__name__}: {exc}"
                self._logger.exception(
                    "plugin %s crashed handling %s on %s",
                    runtime.name, type(event).name, event.account,
                )

    # ------------------------------------------------------------ internals
    def _runtime(self, account: str, plugin: str) -> PluginRuntime:
        runtimes = self._accounts.get(account)
        if runtimes is None or plugin not in runtimes:
            known = self.allowed_names(account) or self.names()
            raise PluginError(
                f"account {account!r} has no plugin {plugin!r} (known: {known})"
            )
        return runtimes[plugin]

    async def _hook(self, account: str, runtime: PluginRuntime, fn: Any, ctx: Any, hook: str) -> None:
        """Await one lifecycle hook; record + re-raise failures on the runtime."""
        try:
            await fn(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.error_count += 1
            runtime.last_error = f"{hook}: {type(exc).__name__}: {exc}"
            self._logger.exception(
                "%s failed for %s:%s", hook, account, runtime.name
            )
            raise

    @staticmethod
    def _services_factory(services: Any) -> Callable[[str], Any]:
        """Accept a services object OR a (plugin_name) -> services factory."""
        if callable(services) and not hasattr(services, "send_message"):
            return services
        return lambda _name: services

    def _context(self, account: str, runtime: PluginRuntime, services: Any) -> Any:
        from core.plugin_base import PluginContext

        return PluginContext(
            account=account,
            plugin=runtime.name,
            settings=runtime.settings,
            services=services,
            logger=logging.getLogger(f"slaughtermudae.{account}.{runtime.name}"),
        )


__all__ = ["PluginManager", "PluginRuntime"]
