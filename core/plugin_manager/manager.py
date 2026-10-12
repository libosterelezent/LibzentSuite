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
import types
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from core.errors import PluginError, SettingsValidationError
from core.dashboard_ui import DashboardUIToolset, DashboardUIRegistry
from core.event_bus import (
    EventBus,
    Event,
    MessageDeleted,
    MessageEdited,
    MessageReceived,
    ReactionAdded,
    PluginEnabledChanged,
    PluginLoaded,
    PluginUnloaded,
)
from core.plugin_base import (
    CommandContext, CommandSpec, parse_command_args, usage_line,
    PluginBase,
)
from core.priority import Priority
from core.settings_manager import SettingsManager

from .commands import COMMAND_HUB, MAX_REPLY_CHARS, FRAMEWORK_PLUGIN
from .commands import RegisteredCommand
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
    #: PHASE-4 (S7-L1): cached dispatch context (rebuilt whenever services
    #: are unwired — deactivated/reattached — via the services-is-None check)
    ctx: Any = None


class PluginManager:
    """Discovery + lifecycle + event dispatch for every account's plugins."""

    def __init__(
        self,
        bus: EventBus,
        logger: logging.Logger,
        settings: SettingsManager,
        plugin_repo: Any = None,
        plugin_dir: str = "plugins",
        *,
        credential_service: Any = None,
        cooldowns: Any = None,
        plugin_state: Any = None,
        dispatch_grace_s: float = 1.0,
        ui_registry: Any = None,
    ) -> None:
        """``credential_service``/``cooldowns``/``plugin_state`` accept the
        framework's shared services (CredentialService, CooldownService,
        PluginStateService) for symmetry with the AccountManager wiring;
        they are stored as attributes (defaults None — back-compatible) and
        the per-plugin facades the plugins actually use are built by the
        account manager's services factory. ``dispatch_grace_s`` bounds how
        long :meth:`deactivate_account` waits for in-flight dispatch tasks
        before cancelling them (round-3 ghost-write finding).
        ``ui_registry`` overrides the plugin dashboard-UI registry (tests);
        a fresh :class:`DashboardUIRegistry` is built by default.
        """
        self._bus = bus
        self._logger = logger
        self._settings = settings
        self._repo = plugin_repo            # database PluginRepository (optional)
        self._loader = PluginLoader(plugin_dir)
        self.credential_service = credential_service
        self.cooldowns = cooldowns
        self.plugin_state = plugin_state
        #: account -> plugin name -> PluginRuntime
        self._accounts: Dict[str, Dict[str, PluginRuntime]] = {}
        #: account -> services factory: (plugin_name) -> PluginServices
        self._services: Dict[str, Callable[[str], Any]] = {}
        #: strong refs for fire-and-forget dispatch tasks (no GC mid-flight)
        self._pending: Set[asyncio.Task] = set()
        #: account -> in-flight dispatch tasks (quiesced on deactivation)
        self._pending_by_account: Dict[str, Set[asyncio.Task]] = {}
        #: bounded grace for in-flight handlers during deactivate_account
        self._dispatch_grace_s = dispatch_grace_s
        #: central registry for plugin-attached dashboard UIs (Stage C);
        #: the dashboard routes dispatch through this in Stage D
        self._ui_registry = ui_registry or DashboardUIRegistry()
        # every account-scoped Discord event type routes to plugins; new
        # types are ADDITIVE — plugins filter by isinstance in handle_event
        self._unsubscribe = bus.subscribe(MessageReceived, self._on_message)
        self._unsub_edited = bus.subscribe(MessageEdited, self._on_message)
        self._unsub_deleted = bus.subscribe(MessageDeleted, self._on_message)
        self._unsub_reaction = bus.subscribe(ReactionAdded, self._on_message)
        self._discovered = False
        # self-command registry: this manager is the process's registry
        # source (last-wins across test-built managers — see commands.py).
        # The attribute form lets tests substitute a private hub.
        self.command_hub = COMMAND_HUB
        COMMAND_HUB.bind(self)

    # ------------------------------------------------------------- discovery
    def discover(self) -> Dict[str, Any]:
        """Scan the plugin dir (raises PluginError on inconsistency).

        Purely synchronous filesystem work — NO repository writes happen
        here; await :meth:`persist_metadata` afterwards (the bootstrap does)
        to refresh the plugin metadata rows the Plugins page reads even
        before any account attaches.
        """
        records = self._loader.discover()
        self._discovered = True
        return records

    async def persist_metadata(self, repo: Any = None) -> int:
        """Persist the discovered plugins' metadata rows (boot path).

        ``repo`` defaults to the constructor's ``plugin_repo``; passing a
        repository explicitly is supported for tests. Returns how many
        manifest rows were written. Best-effort per plugin: one failing
        row is logged and skipped, never fatal.
        """
        target = repo if repo is not None else self._repo
        if target is None:
            return 0
        self._ensure_discovered()
        written = 0
        for name in self._loader.names():
            try:
                rec = self._loader.record(name)
                await target.upsert_metadata(
                    name,
                    rec.cls.version,
                    rec.cls.description,
                    [s.as_manifest() for s in rec.cls.settings],
                )
                written += 1
            except Exception:
                self._logger.exception("plugin metadata persist failed for %s", name)
        return written

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
    async def attach_account(self, account: str,
                             allowed: Optional[Set[str]] = None) -> None:
        """Instantiate this account's plugins (profile allowlist applies).

        ``allowed=None`` keeps every discovered plugin; otherwise only names in
        the set survive (union of the account's profiles, computed by the
        account manager). Settings resolve later, at activate time. Async:
        the persisted enabled-state of each (account, plugin) pair is read
        from the repository here.

        FIX (S4): deactivates PREVIOUS runtimes for this account before
        replacing them, so a failed-start → restart cycle doesn't leave
        orphaned plugin instances running.
        """
        # deactivate old runtimes (fire on_disable/on_unload on any that
        # were active — a restart after a failed start may have left them)
        if account in self._accounts:
            await self.deactivate_account(account)
        self._ensure_discovered()
        wanted = [n for n in self._loader.names() if allowed is None or n in allowed]
        runtimes: Dict[str, PluginRuntime] = {}
        for name in wanted:
            enabled = True
            if self._repo is not None:
                try:
                    enabled = await self._repo.state(account, name)
                    if enabled is None:
                        enabled = True
                except Exception:
                    self._logger.exception("plugin state read failed for %s:%s", account, name)
            try:
                instance = self._loader.create(name)
            except Exception as exc:
                runtime = PluginRuntime(name=name, instance=None, enabled=False)
                runtime.last_error = f"init: {type(exc).__name__}: {exc}"
                runtime.error_count += 1
                runtimes[name] = runtime
                self._logger.exception(
                    "plugin %s failed to instantiate on %s; skipped", name, account
                )
                continue
            runtimes[name] = PluginRuntime(
                name=name, instance=instance, enabled=bool(enabled)
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
        # self-command registry tracks the (assigned ∧ enabled) set — the
        # same hook covers assignment mutations (the account manager
        # re-attaches on every one) and plain (re)attaches
        self._sync_commands(account)

    def reload_plugin(self, name: str) -> Dict[str, Any]:
        """Hot-reload one plugin: detach → re-import → re-attach to accounts.

        Preserves per-account assignments and enabled flags. The plugin's
        dashboard UI is re-registered at boot level. Returns a summary
        dict. Raises PluginError when the plugin is unknown or the
        re-import fails (the OLD state is preserved on failure).
        """
        if name == "__rescan__":
            # special key: full re-scan without targeting one plugin
            self._loader.discover()
            self.init_boot_dashboard_uis()
            found = self._loader.names()
            self._logger.info("plugin directory rescan: %d plugin(s)", len(found))
            return {"plugin": "__rescan__", "discovered": found,
                    "restored_accounts": 0}
        # always re-scan: an unloaded plugin was removed from _records,
        # so we need a fresh discovery to pick it up again
        self._loader.discover()
        if name not in self._loader.names():
            raise PluginError(f"unknown plugin {name!r}")
        # snapshot the affected accounts so we can restore
        affected = {
            acct: dict((pname, rt) for pname, rt in runtimes.items()
                       if pname == name)
            for acct, runtimes in self._accounts.items() if name in runtimes
        }
        # detach the plugin from every account (preserving other plugins)
        for acct in list(affected):
            rt = self._accounts.get(acct, {}).pop(name, None)
            if rt is not None and rt.active:
                # fire on_disable + on_unload for a clean teardown
                try:
                    if rt.instance is not None:
                        import asyncio
                        if asyncio.get_event_loop().is_running():
                            # can't await here (sync method) — the hooks
                            # fire on next activation instead
                            pass
                except Exception:
                    pass
        # unregister the UI
        self._ui_registry.detach(name)
        # re-import: remove from loader's records, re-scan, pick up the
        # fresh module
        self._loader._records.pop(name, None)
        self._loader.discover()   # re-scan picks up the changed module
        if name not in self._loader.names():
            raise PluginError(
                f"plugin {name!r} disappeared after re-scan — the old "
                f"instance was detached")
        # re-register the boot UI
        self.init_boot_dashboard_uis()
        # re-attach to every affected account
        restored = 0
        for acct, was in affected.items():
            if acct not in self._accounts:
                continue   # account gone while we worked
            old_rt = was.get(name)
            rt = PluginRuntime(
                name=name, instance=None,
                enabled=old_rt.enabled if old_rt else True,
                loaded=False, active=False)
            self._accounts[acct][name] = rt
            restored += 1
        self._logger.info(
            "plugin %s reloaded (restored on %d account(s))", name, restored)
        return {"plugin": name, "restored_accounts": restored}

    def unload_plugin(self, name: str) -> Dict[str, Any]:
        """Unload one plugin: detach from every account, unregister UI.

        Per-account settings and ASSIGNMENTS are preserved in the
        database — reload_plugin restores them. The plugin is removed
        from the discovered set and will not be offered again until
        discover() re-scans or reload_plugin is called.
        """
        if not self._discovered:
            self.discover()
        detached = 0
        for acct, runtimes in list(self._accounts.items()):
            rt = runtimes.get(name)
            if rt is not None:
                rt.loaded = False
                rt.active = False
                rt.instance = None
                detached += 1
        self._ui_registry.detach(name)
        # remove from the loader so it's not re-offered
        self._loader._records.pop(name, None)
        self._logger.info(
            "plugin %s unloaded (detached from %d account(s))", name, detached)
        return {"plugin": name, "detached_accounts": detached}

    def detach_account(self, account: str) -> None:
        """Drop the account's instances WITHOUT firing unload hooks.

        The account manager calls deactivate_account first when the plugins
        were active; detach only clears bookkeeping.
        """
        self._accounts.pop(account, None)
        self._services.pop(account, None)
        # dispatch bookkeeping: any task still queued dies at the services
        # gate in _dispatch (the account's services are gone)
        self._pending_by_account.pop(account, None)
        # the account's self-command surface is gone with it
        self.command_hub.forget(account)

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
        # UI lifecycle: offer the toolset to plugins that declare a UI
        # (after on_load/on_enable — actions may reflect plugin state)
        self._init_dashboard_uis(account, runtimes)
        self._logger.info("activated plugins on %s", account)

    def init_boot_dashboard_uis(self) -> int:
        """Register plugin dashboard UIs at DISCOVERY time (no account).

        Instantiates a THROWAWAY instance of each discovered plugin that
        overrides initialize_dashboard_ui, calls the hook, and discards
        the instance — the layout is class-level state, not per-account.
        This makes plugin UI pages visible in the dashboard from boot,
        not only after an account connects. Returns how many UIs were
        registered. Failures are isolated per plugin.
        """
        registered = 0
        if not self._discovered:
            self.discover()
        for plugin_name, record in self._loader._records.items():
            if plugin_name in self._ui_registry.list_plugins():
                continue  # already registered (e.g. by an account)
            cls = record.cls
            # check the CLASS for an overridden hook (no instance needed)
            if getattr(cls, "initialize_dashboard_ui", None)                     is getattr(PluginBase, "initialize_dashboard_ui", None):
                continue  # base no-op: declares no UI
            try:
                instance = cls()
                instance._ui_registry = self._ui_registry
                from core.dashboard_ui import DashboardUIToolset
                instance.initialize_dashboard_ui(DashboardUIToolset())
                registered += 1
            except Exception:
                self._logger.warning(
                    "boot UI init failed for %s — isolated", plugin_name,
                    exc_info=True)
                self._ui_registry.detach(plugin_name)
        if registered:
            self._logger.info(
                "boot dashboard UIs registered: %d plugin(s)", registered)
        return registered

    def _init_dashboard_uis(self, account: str,
                            runtimes: Dict[str, PluginRuntime]) -> None:
        """Offer the dashboard UI toolset to this account's plugins.

        Only plugins that OVERRIDE ``initialize_dashboard_ui`` are called
        (the PluginBase no-op means "declares no UI"). Plugin-wide dedup:
        a plugin's UI registers ONCE — the first account to activate it
        wins, later activations of the same plugin on other accounts see
        the registration and skip (one dashboard page per plugin, not per
        account). Failures are isolated: a crashing hook (or a rejected
        layout) never fails the account's activation, and any partial
        registration is detached again.
        """
        for plugin_name, entry in runtimes.items():
            instance = entry.instance
            if instance is None or not entry.enabled:
                continue
            hook = getattr(instance, "initialize_dashboard_ui", None)
            if hook is None or not callable(hook):
                continue  # duck-typed instance without the hook
            # bound-method vs function never compares identical: check the
            # CLASS attribute to see whether the hook is actually overridden
            if getattr(type(instance), "initialize_dashboard_ui", None) \
                    is PluginBase.initialize_dashboard_ui:
                continue  # base no-op: this plugin declares no UI
            if plugin_name in self._ui_registry.list_plugins():
                continue  # another account already registered this UI
            try:
                # inject the registry reference attach_dashboard_ui uses
                instance._ui_registry = self._ui_registry
                hook(DashboardUIToolset())
            except Exception as exc:
                entry.error_count += 1
                entry.last_error = f"ui_init: {type(exc).__name__}: {exc}"
                self._logger.warning(
                    "dashboard UI init failed for %s on %s — isolated",
                    plugin_name, account, exc_info=True)
                # don't fail the activation — clean any partial registration
                self._ui_registry.detach(plugin_name)

    async def _activate_one(self, account: str, runtime: PluginRuntime) -> None:
        """Load + enable one runtime, isolating any hook failure."""
        if runtime.instance is None:
            # constructor failed at attach: nothing to drive. Disable (a
            # later set_enabled(True) retry must not leave an "enabled but
            # dead" runtime) and keep the original init error visible.
            runtime.enabled = False
            self._logger.error(
                "plugin %s on %s has no instance (constructor failed); "
                "cannot activate", runtime.name, account,
            )
            return
        services = self._services.get(account, lambda _n: None)(runtime.name)
        runtime.ctx = None  # PHASE-4 (S7-L1): new facade -> rebuild cache
        try:
            resolved = self._settings.resolve(
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
        # update IN PLACE, never rebind: the services facade (captured above)
        # and every context already hold THIS dict — a fresh object would
        # fork the documented live-settings view
        runtime.settings.clear()
        runtime.settings.update(resolved)
        ctx = self._context(account, runtime, services,
                            explicit=self._explicit_keys(account, runtime.name))
        first_load = not runtime.loaded
        if first_load:
            # DOUBLE-FIRE GUARD (round-3): mark loaded BEFORE awaiting
            # on_load. Two racing activations (e.g. a set_enabled(True)
            # landing on an await point of activate_account) used to both
            # read ``loaded=False`` and fire on_load twice.
            runtime.loaded = True
        load_done = False
        try:
            if first_load:
                await self._hook(account, runtime, runtime.instance.on_load, ctx, "on_load")
                load_done = True
            await self._hook(account, runtime, runtime.instance.on_enable, ctx, "on_enable")
            runtime.active = True
            if first_load:  # a re-enable is not a load: PluginEnabledChanged
                self._bus.publish_nowait(PluginLoaded(account=account, plugin=runtime.name))
        except asyncio.CancelledError:
            if first_load and not load_done:
                runtime.loaded = False  # cancelled mid-load: retry-able
            raise
        except Exception:
            if first_load and not load_done:
                runtime.loaded = False  # a failed load stays retry-able
            runtime.enabled = False  # _hook already recorded the error
            self._logger.error(
                "plugin %s failed to activate on %s; disabled", runtime.name, account
            )

    def close(self) -> None:
        """Tear the manager down: bus subscriptions off, pending tasks reaped.

        Called by Framework.shutdown after accounts stopped — a lingering
        subscription set would keep dispatching to dead plugin runtimes.
        """
        for unsub in (self._unsubscribe, self._unsub_edited,
                      self._unsub_deleted, self._unsub_reaction):
            try:
                unsub()
            except Exception:
                pass
        for task in [t for t in list(self._pending) if not t.done()]:
            task.cancel()
        self._pending_by_account.clear()
        COMMAND_HUB.unbind(self)  # self-command invocations route nowhere now

    async def deactivate_account(self, account: str) -> None:
        """Fire on_disable + on_unload for the account's active plugins.

        The services entry is popped BEFORE the hook loop (the factory is
        captured locally): a ``set_enabled(account, ..., True)`` landing on
        an await point mid-deactivate must see ``account not in
        self._services`` and NOT fire activation hooks into an account that
        is going down (otherwise a plugin could be left ``active`` on a
        stopped account).
        """
        runtimes = self._accounts.get(account)
        if runtimes is None:
            return
        factory = self._services.pop(account, None)
        # GHOST-WRITE GUARD (round-3): bounded-join this account's in-flight
        # dispatch tasks BEFORE the hooks run. A handler that outlives the
        # deactivation could cooldown_set/state_set for a REMOVED account
        # AFTER the account manager's removal cascades ran — ghost rows a
        # re-created same-name account would silently inherit.
        await self._quiesce_dispatch(account)
        for runtime in runtimes.values():
            services = factory(runtime.name) if factory is not None else None
            ctx = self._context(account, runtime, services)
            runtime.ctx = None  # PHASE-4 (S7-L1): services unwired below
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
                runtime.loaded = False
                self._bus.publish_nowait(PluginUnloaded(account=account, plugin=runtime.name))
            runtime.active = False
        # UI cleanup: detach this account's plugin UIs — but a plugin's UI
        # is PLUGIN-WIDE, so only detach when NO other account still has
        # the plugin active (ref-counted by account)
        for plugin_name in list(runtimes.keys()):
            if self._ui_active_elsewhere(plugin_name, excluding=account):
                continue
            self._ui_registry.detach(plugin_name)

    def _ui_active_elsewhere(self, plugin: str, excluding: str) -> bool:
        """True when any OTHER account still holds this plugin active
        (enabled + activated) — its UI registration must survive."""
        for acct, others in self._accounts.items():
            if acct == excluding:
                continue
            rt = others.get(plugin)
            if rt is None or rt.instance is None:
                continue
            if rt.enabled and rt.active:
                return True
        return False

    @property
    def ui_registry(self) -> Any:
        """The plugin dashboard-UI registry (Stage D routes dispatch here)."""
        return self._ui_registry

    async def _quiesce_dispatch(self, account: str) -> None:
        """Bounded-join the account's in-flight dispatch tasks.

        Waits up to ``dispatch_grace_s`` for every running handler, then
        cancels the stragglers and reaps them — deactivation (and the
        account manager's removal cascades that follow it) must be the
        LAST writer for the account, never racing a parked handler.
        """
        tasks = self._pending_by_account.get(account)
        if not tasks:
            return
        in_flight = [t for t in tasks if not t.done()]
        if not in_flight:
            return
        _, stragglers = await asyncio.wait(in_flight, timeout=self._dispatch_grace_s)
        for task in stragglers:
            task.cancel()
        if stragglers:
            self._logger.info(
                "cancelled %d dispatch task(s) still running on %s after "
                "%.2fs grace", len(stragglers), account, self._dispatch_grace_s,
            )
            await asyncio.gather(*stragglers, return_exceptions=True)

    # ------------------------------------------------------------- mutation
    async def set_enabled(self, account: str, plugin: str, enabled: bool) -> None:
        """Toggle a plugin at runtime (persisted BEFORE memory mutates)."""
        runtime = self._runtime(account, plugin)
        if self._repo is not None:
            await self._repo.set_state(account, plugin, bool(enabled))
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
            PluginEnabledChanged(
                account=account, plugin=plugin, enabled=runtime.enabled
            )
        )
        # enabled state is part of command visibility — re-publish
        self._sync_commands(account)

    async def set_setting(self, account: str, plugin: str, key: str, value: Any) -> Any:
        """Validate + persist + apply one plugin setting for one account."""
        runtime = self._runtime(account, plugin)
        schema = self._schema_for(plugin)
        validated = await self._settings.set_plugin_setting(
            account, plugin, key, value, schema
        )
        runtime.settings[key] = validated  # live view (contexts share the dict)
        return validated

    async def clear_setting(self, account: str, plugin: str, key: str) -> Any:
        """Remove one account-layer override; live view falls back to the
        next layer (global/plugin default) — mirrors :meth:`set_setting`."""
        runtime = self._runtime(account, plugin)
        schema = self._schema_for(plugin)
        effective = await self._settings.clear_plugin_setting(
            account, plugin, key, schema
        )
        if schema:
            runtime.settings[key] = effective  # live view (shared dict)
        else:
            runtime.settings.pop(key, None)
        return effective

    # ---------------------------------------------------------------- state
    async def ui_stats(self, account: str) -> List[Dict[str, Any]]:
        """Collect every enabled plugin's Home-tab stat cards (JSON-safe).

        Each card is {"plugin", "label", "value", "sub", "level"} — the
        dashboard renders them in its own metric template. A crashing
        provider contributes nothing (isolation).
        """
        cards: List[Dict[str, Any]] = []
        runtime = self._accounts.get(account)
        if not runtime:
            return cards
        services = self._services.get(account)
        for plugin_name, entry in runtime.items():
            instance = getattr(entry, "instance", None)
            provider = getattr(instance, "ui_stats", None)
            if provider is None or not callable(provider):
                continue
            ctx = getattr(instance, "_ctx", None)
            if ctx is None:
                continue
            try:
                for card in (await provider(ctx)) or ():
                    cards.append({
                        "plugin": plugin_name,
                        "label": str(getattr(card, "label", "")),
                        "value": str(getattr(card, "value", "")),
                        "sub": str(getattr(card, "sub", "")),
                        "level": str(getattr(card, "level", "")),
                    })
            except Exception:
                self._logger.warning(
                    "ui_stats failed for %s on %s — skipped",
                    plugin_name, account, exc_info=True)
        return cards

    async def ui_panels(self) -> List[Dict[str, Any]]:
        """Collect every plugin's sidebar panels (JSON-safe, deduped by id).

        Panels are framework-level (not per-account): the first instance
        of each plugin that provides a panel wins (deterministic).
        """
        panels: Dict[str, Dict[str, Any]] = {}
        for account, runtime in self._accounts.items():
            for plugin_name, entry in runtime.items():
                instance = getattr(entry, "instance", None)
                provider = getattr(instance, "ui_panels", None)
                if provider is None or not callable(provider):
                    continue
                ctx = getattr(instance, "_ctx", None)
                if ctx is None:
                    continue
                try:
                    for panel in (await provider(ctx)) or ():
                        pid = str(getattr(panel, "id", ""))
                        if not pid or pid in panels:
                            continue
                        sections = []
                        for sec in (getattr(panel, "sections", ()) or ()):
                            sections.append({
                                "kind": str(getattr(sec, "kind", "text")),
                                "title": str(getattr(sec, "title", "")),
                                "data": getattr(sec, "data", None),
                            })
                        panels[pid] = {
                            "id": pid,
                            "plugin": plugin_name,
                            "title": str(getattr(panel, "title", "")),
                            "icon": str(getattr(panel, "icon", "")),
                            "sections": sections,
                        }
                except Exception:
                    self._logger.warning(
                        "ui_panels failed for %s — skipped",
                        plugin_name, exc_info=True)
        return sorted(panels.values(), key=lambda p: p["id"])

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

    def live_settings(self, account: str, plugin: str) -> Optional[Dict[str, Any]]:
        """The LIVE resolved-settings dict for one (account, plugin) pair.

        Unlike ``states()`` (which returns copies for display), this hands
        out the dict the runtime contexts share — ``set_setting`` mutates it
        in place, so readers see changes immediately. The account manager
        passes it to PluginServices so ``services.settings`` and
        ``ctx.settings`` can never diverge (isolation finding). None when
        the pair is unknown.
        """
        runtime = self._accounts.get(account, {}).get(plugin)
        return runtime.settings if runtime is not None else None

    # ---------------------------------------------------------- self-commands
    def command_entries(self, account: str) -> List[Tuple[CommandSpec, str]]:
        """``(CommandSpec, plugin name)`` pairs for the account's plugins.

        Collection rule (visibility = assigned ∧ enabled): every runtime
        the account holds that is enabled and instantiated contributes its
        class's ``COMMANDS``. Handlers declared as plain plugin-class
        methods are BOUND to the per-account instance here (one instance
        per (account, plugin) pair — handlers reach plugin internals like
        any lifecycle hook); an empty ``category`` defaults to the plugin's
        display name.

        Collision policy: deterministic manifest order (plugin name sort);
        the FIRST registration of a command name wins, later collisions
        are dropped with a warning naming both plugins.
        """
        runtimes = self._accounts.get(account, {})
        out: List[Tuple[CommandSpec, str]] = []
        taken: Dict[str, str] = {}
        for plugin_name in sorted(runtimes):  # manifest-sort determinism
            runtime = runtimes[plugin_name]
            if not runtime.enabled or runtime.instance is None:
                continue
            specs = getattr(type(runtime.instance), "COMMANDS", ()) or ()
            for declared in specs:
                if declared.name in taken:
                    self._logger.warning(
                        "self-command %r declared by plugin %r collides with "
                        "%r — first registration wins, the later one is dropped",
                        declared.name, plugin_name, taken[declared.name])
                    continue
                taken[declared.name] = plugin_name
                spec = replace(
                    declared,
                    handler=self._bind_handler(declared.handler,
                                               runtime.instance),
                    category=declared.category or plugin_name,
                )
                out.append((spec, plugin_name))
        return out

    def commands_for(self, account: str) -> List[CommandSpec]:
        """The account's plugin-declared CommandSpecs (bound handlers,
        defaulted categories, collisions resolved) — spec-only view of
        :meth:`command_entries`."""
        return [spec for spec, _plugin in self.command_entries(account)]

    @staticmethod
    def _bind_handler(handler: Any, instance: Any) -> Any:
        """Bind a plain plugin-class method to the per-account instance.

        A handler declared as a method (``async def _cmd(self, ctx, args)``
        referenced bare in the class-level ``COMMANDS`` tuple) arrives as
        an unbound function; descriptor-binding gives it ``self`` so it
        reaches the plugin's own state like a lifecycle hook. The class-
        body provenance check (walking the MRO) keeps FREE functions and
        lambdas — which already have the right signature — untouched.
        """
        if isinstance(handler, types.FunctionType):
            for klass in type(instance).__mro__:
                if handler in vars(klass).values():
                    return handler.__get__(instance, klass)
        return handler

    def _sync_commands(self, account: str) -> None:
        """Re-publish the account's self-command surface (hub → client)."""
        try:
            self.command_hub.refresh(account)
        except Exception:  # registry sync must never fail an attach/toggle
            self._logger.exception("self-command sync failed for %s", account)

    # ------------------------------------------------------------ execution
    async def execute_self_command(
        self,
        account: str,
        name: str,
        args: List[str],
        channel_id: int,
        *,
        guild_id: Optional[int] = None,
        invoker_id: Optional[int] = None,
        raw: str = "",
    ) -> None:
        """Run one self-command and reply in-channel through the pipeline.

        Fully error-contained (a crashing handler must never kill the
        account or its Bot): the handler's string return value, its usage
        errors (``ValueError`` — including ``SettingsValidationError``)
        and the internal-error fallback are ALL sent back through the
        per-plugin services facade — the same paced pipeline plugin sends
        use — never a raw client send.

        The owner gate mirrors the trigger-word path exactly: the invoking
        Discord user id must pass AccessManager.can_invoke (when wired)
        or the credentials view's owner ids (``services.credentials`` —
        the legacy fallback for direct-construction stacks) — non-owners
        are ignored with no reply, like today.

        FRAMEWORK commands (plugin="selfcmd": help/pg/estop/…) run even
        when the account's plugins are deactivated — their handlers use
        the hub's api_provider, not ctx.services, and an undeliverable
        reply is logged instead of dropped (SAFETY: `estop` always runs).
        """
        entry = self._find_command(account, name)
        if entry is None:
            self._logger.debug("unknown self-command %r on %s", name, account)
            return
        spec, plugin = entry.spec, entry.plugin
        factory = self._services.get(account)
        services = factory(plugin) if factory is not None else None
        if services is None and plugin != FRAMEWORK_PLUGIN:
            # Plugin verbs need their paced pipeline. FRAMEWORK verbs do
            # not (their handlers talk to the ControlAPI through the
            # hub's api_provider): estop must run even on an account
            # whose plugins are deactivated — only the REPLY needs the
            # pipeline, and _send_reply logs it when there is none.
            self._logger.warning(
                "self-command %r on %s dropped: %s", name, account,
                "plugins not activated" if factory is None
                else "no services facade")
            return
        # THE command gate: AccessManager.can_invoke (per-account
        # permission list + admin bypass, fail-closed) when wired; the
        # credentials view's owner ids are the LEGACY fallback for
        # direct-construction stacks without an access manager.
        if invoker_id is not None:
            access = getattr(self, "_access_manager", None)
            if access is None and services is None:
                # framework verb with NO plugin pipeline (estop on a
                # deactivated account): there are no facade credentials
                # to read the legacy owner list from — consult the hub's
                # bootstrap-registered AccessManager instead, so the
                # safety verbs keep their gate without widening it
                access = getattr(self.command_hub, "access", None)
            allowed = False
            if access is not None:
                try:
                    allowed = await access.can_invoke(account, invoker_id)
                except Exception:
                    self._logger.exception(
                        "access check failed for %r on %s — failing closed",
                        invoker_id, account)
                    allowed = False
            else:
                owners = getattr(
                    getattr(services, "credentials", None),
                    "owner_user_ids", ()) or ()
                allowed = invoker_id in set(owners)
            if not allowed:
                self._logger.info(
                    "self-command %r on %s ignored: invoker %s not allowed",
                    name, account, invoker_id)
                return
        ctx = CommandContext(
            account=account,
            plugin=plugin,
            services=services,
            logger=logging.getLogger(
                f"slaughtermudae.{account}.{plugin}.command"),
            channel_id=int(channel_id or 0),
            guild_id=guild_id,
            settings_api=self._settings_api(account, plugin),
            raw=raw,
            parsed={},
            invoker_id=invoker_id,
        )
        try:
            if spec.handler is None:
                raise PluginError(f"command {spec.name!r} declares no handler")
            if spec.args:
                # declarative ArgSpecs: parse/convert/validate BEFORE the
                # handler — failures render the auto-usage line below
                ctx.parsed.update(
                    parse_command_args(spec, list(args or [])))
            reply = await spec.handler(ctx, list(args or []))
        except asyncio.CancelledError:
            raise
        except (ValueError, SettingsValidationError) as exc:
            # usage error: render the usage line + the reason back
            usage = f"{spec.name} {spec.usage or usage_line(spec)}".strip()
            reply = f"usage: {usage}\n{exc}" if str(exc) else f"usage: {usage}"
        except Exception:
            ctx.logger.exception("self-command %s crashed on %s", name, account)
            reply = f"error: {name} failed internally (see logs)"
        if not reply:
            return
        await self._send_reply(services, channel_id, str(reply), ctx.logger)

    async def _send_reply(self, services: Any, channel_id: int, text: str,
                          logger: logging.Logger) -> None:
        """One paced reply (NORMAL priority — interactive, single message)."""
        if len(text) > MAX_REPLY_CHARS:
            text = text[: MAX_REPLY_CHARS - 20] + "\n… (truncated)"
        if services is None:
            # framework verb (estop!) on an account with no active plugin
            # pipeline: the reply CANNOT be delivered in-band — log it at
            # ERROR with the full text so the operator's answer survives
            # (the retired remote control's undeliverable-reply contract)
            logger.error(
                "self-command reply undeliverable (no services facade) — "
                "reply was: %s", text)
            return
        try:
            await services.send_message(
                int(channel_id), text, priority=Priority.NORMAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            # the reply is best-effort exactly like the remote-control
            # bridge: log it loudly so the operator's answer survives
            logger.error("self-command reply undeliverable — reply was: %s",
                         text)

    def _find_command(self, account: str,
                      name: str) -> Optional[RegisteredCommand]:
        """Registry lookup from THIS manager's own view (never the hub's —
        the hub may be rebound by a newer manager in tests): framework
        verbs (reserved) first, then the account's plugin-declared specs."""
        from .commands import FRAMEWORK_COMMANDS, FRAMEWORK_PLUGIN

        for spec in FRAMEWORK_COMMANDS:
            if spec.name == name:
                return RegisteredCommand(spec=spec, plugin=FRAMEWORK_PLUGIN)
        for spec, plugin in self.command_entries(account):
            if spec.name == name:
                return RegisteredCommand(spec=spec, plugin=plugin)
        return None

    # ------------------------------------------------- command settings api
    def _settings_api(self, account: str, plugin: str) -> Any:
        """The validated settings surface for command handlers (cached
        per call site is unnecessary — construction is trivial)."""
        return _ManagerSettingsApi(self, account, plugin)

    # -------------------------------------------------------------- routing
    def _on_message(self, event: Event) -> None:
        """Bus handler (sync): fan a MessageReceived out as a fire-and-forget
        dispatch task so the publisher is never blocked by a slow plugin."""
        if not isinstance(event, (MessageReceived, MessageEdited,
                                  MessageDeleted, ReactionAdded)):
            return
        runtimes = self._accounts.get(event.account)
        if not runtimes:
            return
        task = asyncio.get_running_loop().create_task(self._dispatch(event, runtimes))
        self._pending.add(task)
        self._pending_by_account.setdefault(event.account, set()).add(task)
        task.add_done_callback(
            lambda t, account=event.account: self._reap_dispatch(account, t)
        )

    def _reap_dispatch(self, account: str, task: asyncio.Task) -> None:
        """Drop a finished dispatch task from both bookkeeping sets."""
        self._pending.discard(task)
        tasks = self._pending_by_account.get(account)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._pending_by_account.pop(account, None)

    async def _dispatch(self, event: MessageReceived, runtimes: Dict[str, PluginRuntime]) -> None:
        """Await every enabled plugin's handle_event, isolated per plugin.

        The ``runtimes`` dict is a snapshot taken at enqueue time; if the
        account was detached (or re-attached with fresh runtimes) before
        this task got scheduled, the snapshot is STALE and dispatching to
        it would fire handlers on an account that no longer exists — skip.

        SERVICES GATE (round-3): ``deactivate_account`` pops the services
        entry BEFORE its hook loop runs; a dispatch task that starts inside
        that window used to pass the enabled+active gate, resolve
        ``services=None`` and crash the handler with an AttributeError the
        plugin got blamed for. Skip instead — the account is going down.
        """
        if self._accounts.get(event.account) is not runtimes:
            return
        factory = self._services.get(event.account)
        if factory is None:
            return
        for runtime in runtimes.values():
            if not runtime.enabled or not runtime.active:
                continue
            # PHASE-4 (S7-L1): the context (logger lookup + dataclass) is
            # cached per runtime — all its inputs are stable per activation
            # (settings stays the LIVE dict, services the same facade), and
            # rebuilding it per event × per plugin sat on the hottest path.
            # (round-3: a ctx rebuilt with no services mid-deactivate is
            # SKIPPED, never delivered — the plugin would crash on None)
            ctx = runtime.ctx
            if ctx is None or ctx.services is None:
                services = self._services.get(
                    event.account, lambda _n: None)(runtime.name)
                ctx = runtime.ctx = self._context(
                    event.account, runtime, services)
                if ctx.services is None:
                    continue
            try:
                await runtime.instance.handle_event(ctx, event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                runtime.error_count += 1
                runtime.last_error = f"{type(event).__name__}: {type(exc).__name__}: {exc}"
                self._logger.exception(
                    "plugin %s crashed handling %s on %s",
                    runtime.name, type(event).__name__, event.account,
                )

    # ------------------------------------------------------------ internals
    def _explicit_keys(self, account: str, plugin: str) -> Optional[FrozenSet[str]]:
        """Setting keys the user set EXPLICITLY in any settings layer.

        A resolved value equal to the schema default is ambiguous: it may
        be the default OR an explicit user choice that happens to match it
        (a plugin's runtime auto-disable vs. an explicit re-enable,
        round-3). The layer VIEWS carry the distinction; None when the
        settings manager exposes no views (stripped-down test stacks).
        """
        views = getattr(self._settings, "layers_view", None)
        if not callable(views):
            return None
        try:
            layers = views()
        except Exception:
            return None
        explicit = set(layers.get("global", {}).get(plugin, {}))
        explicit.update(
            layers.get("accounts", {}).get(account, {}).get(plugin, {})
        )
        return frozenset(explicit)

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

    def _context(self, account: str, runtime: PluginRuntime, services: Any,
                 explicit: Optional[frozenset] = None) -> Any:
        from core.plugin_base import PluginContext

        return PluginContext(
            account=account,
            plugin=runtime.name,
            settings=runtime.settings,
            services=services,
            logger=logging.getLogger(f"slaughtermudae.{account}.{runtime.name}"),
            explicit_settings=explicit,
        )


class _ManagerSettingsApi:
    """CommandSettingsApi over the plugin manager's own surfaces.

    ``set``/``clear`` go through :meth:`PluginManager.set_setting` /
    ``clear_setting` — the SAME validation path the ControlAPI's
    ``plugins_set_setting`` uses (parity by construction). ``effective``
    reproduces the Phase-1 account view (``accounts_effective_settings``)
    through the managers, never HTTP: plugin Setting defaults <- the
    bound profile's settings (minus the reserved ``channels`` key) <- the
    account's per-plugin overrides, with the profile's channel scope
    riding along under ``"channels"``.

    Profile/binding repositories are reached duck-typed through the
    plugin repository's owning database (``BaseRepository._db`` — the
    documented back-reference every repository holds); stacks without
    profile storage degrade to the settings-manager view with
    ``channels = None``.
    """

    def __init__(self, manager: "PluginManager", account: str,
                 plugin: str) -> None:
        self._manager = manager
        self._account = account
        self._plugin = plugin

    # ------------------------------------------------------------- repos
    @staticmethod
    def _repos(manager: "PluginManager") -> tuple:
        """(plugin_profiles repo, account_plugins repo) — either None."""
        database = getattr(manager._repo, "_db", None)  # BaseRepository
        return (getattr(database, "plugin_profiles", None),
                getattr(database, "account_plugins", None))

    # -------------------------------------------------------------- api
    def schema(self) -> tuple:
        return tuple(self._manager._schema_for(self._plugin))

    async def binding(self) -> Optional[str]:
        profile_repo, binding_repo = self._repos(self._manager)
        reader = getattr(binding_repo, "bindings", None)
        if not callable(reader):
            return None
        try:
            return (await reader(self._account) or {}).get(self._plugin)
        except Exception:
            return None

    async def profiles(self) -> List[Dict[str, Any]]:
        profile_repo, _binding_repo = self._repos(self._manager)
        lister = getattr(profile_repo, "list", None)
        if not callable(lister):
            return []
        try:
            records = await lister(self._plugin) or []
        except Exception:
            return []
        return [
            {
                "name": getattr(r, "name", "?"),
                "description": getattr(r, "description", "") or "",
            }
            for r in records
        ]

    async def effective(self) -> Dict[str, Any]:
        from core import plugin_scopes

        schema = self.schema()
        defs = {s.name: s for s in schema}
        merged: Dict[str, Any] = {s.name: s.validate(s.default) for s in schema}
        reserved = plugin_scopes.CHANNELS_KEY
        profile_name: Optional[str] = None
        scope: Optional[Dict[str, Any]] = None
        # bound profile layer (defaults <- profile)
        profile_repo, binding_repo = self._repos(self._manager)
        reader = getattr(binding_repo, "bindings", None)
        if callable(reader):
            try:
                profile_name = (await reader(self._account) or {}).get(self._plugin)
            except Exception:
                profile_name = None
        getter = getattr(profile_repo, "get", None)
        if profile_name is not None and callable(getter):
            try:
                record = await getter(self._plugin, profile_name)
            except Exception:
                record = None
            if record is not None:  # dangling binding: skip (parity)
                for key, value in (getattr(record, "settings", None) or {}).items():
                    if key != reserved and key in defs:
                        merged[key] = value
                raw_scope = (getattr(record, "settings", None) or {}).get(reserved)
                try:
                    scope = plugin_scopes.validate(raw_scope) \
                        if raw_scope is not None else None
                except ValueError:
                    scope = None
        # account overrides (highest); the scope is profile-owned
        try:
            layers = self._manager._settings.layers_view()
            overrides = (layers.get("accounts", {}).get(self._account, {})
                         .get(self._plugin, {}))
        except Exception:
            overrides = {}
        for key, value in overrides.items():
            if key == reserved or key not in defs:
                continue
            try:
                merged[key] = defs[key].validate(value)
            except SettingsValidationError:
                merged[key] = value  # parity: the API layer applies raw
        merged[reserved] = scope
        return merged

    async def set(self, key: str, value: Any) -> Any:
        """Same validation path as /api/plugins/set-setting."""
        return await self._manager.set_setting(self._account, self._plugin,
                                               key, value)

    async def clear(self, key: str) -> Any:
        return await self._manager.clear_setting(self._account, self._plugin,
                                                 key)


__all__ = ["PluginManager", "PluginRuntime"]
