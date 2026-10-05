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
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set

from core.errors import PluginError
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
    ) -> None:
        """``credential_service``/``cooldowns``/``plugin_state`` accept the
        framework's shared services (CredentialService, CooldownService,
        PluginStateService) for symmetry with the AccountManager wiring;
        they are stored as attributes (defaults None — back-compatible) and
        the per-plugin facades the plugins actually use are built by the
        account manager's services factory. ``dispatch_grace_s`` bounds how
        long :meth:`deactivate_account` waits for in-flight dispatch tasks
        before cancelling them (round-3 ghost-write finding).
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
        # every account-scoped Discord event type routes to plugins; new
        # types are ADDITIVE — plugins filter by isinstance in handle_event
        self._unsubscribe = bus.subscribe(MessageReceived, self._on_message)
        self._unsub_edited = bus.subscribe(MessageEdited, self._on_message)
        self._unsub_deleted = bus.subscribe(MessageDeleted, self._on_message)
        self._unsub_reaction = bus.subscribe(ReactionAdded, self._on_message)
        self._discovered = False

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
        (roll's userstack auto-disable vs. an explicit re-enable, round-3).
        The layer VIEWS carry the distinction; None when the settings
        manager exposes no views (stripped-down test stacks).
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


__all__ = ["PluginManager", "PluginRuntime"]
