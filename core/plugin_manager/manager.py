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
import contextlib
import logging
import types
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from core import plugin_scopes
from core.errors import PluginError, SettingsValidationError
from core.event_bus import (
    Event,
    EventBus,
    MessageDeleted,
    MessageEdited,
    MessageReceived,
    PluginEnabledChanged,
    PluginLoaded,
    PluginUnloaded,
    ReactionAdded,
)
from core.plugin_base import (
    CommandContext,
    CommandSpec,
    parse_command_args,
    usage_line,
)
from core.priority import Priority

from .commands import COMMAND_HUB, FRAMEWORK_PLUGIN, MAX_REPLY_CHARS, RegisteredCommand
from .loader import PluginLoader

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.settings_manager import SettingsManager

#: "no factory provided" for _teardown_runtime (None is a VALID factory
#: answer — deactivate passes its already-popped factory explicitly)
_UNSET_FACTORY = object()


@dataclass
class PluginRuntime:
    """One plugin instance bound to one account (manager-internal)."""

    name: str
    instance: Any  # PluginBase (duck-typed: has hooks)
    enabled: bool = True
    loaded: bool = False
    active: bool = False  # activate_account ran (services wired)
    settings: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None
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
        dispatch_inflight_limit: int = 100,
    ) -> None:
        """``credential_service``/``cooldowns``/``plugin_state`` accept the
        framework's shared services (CredentialService, CooldownService,
        PluginStateService) for symmetry with the AccountManager wiring;
        they are stored as attributes (defaults None — back-compatible) and
        the per-plugin facades the plugins actually use are built by the
        account manager's services factory. ``dispatch_grace_s`` bounds how
        long :meth:`deactivate_account` waits for in-flight dispatch tasks
        before cancelling them (round-3 ghost-write finding).
        ``ui_registry`` is the optional plugin UI registry (tests);
        None when no UI system is present.
        ``dispatch_inflight_limit`` caps how many dispatch tasks per
        account may be in flight at once (flood bound — see
        :meth:`_on_message`); excess events are dropped-newest and
        counted (:meth:`dispatch_stats`).
        """
        self._bus = bus
        self._logger = logger
        self._settings = settings
        self._repo = plugin_repo  # database PluginRepository (optional)
        self._loader = PluginLoader(plugin_dir)
        self.credential_service = credential_service
        self.cooldowns = cooldowns
        self.plugin_state = plugin_state
        #: account -> plugin name -> PluginRuntime
        self._accounts: dict[str, dict[str, PluginRuntime]] = {}
        #: per-account allowlist from the last attach (None = all), so a
        #: reload can restore an UNLOADED plugin that a later re-attach
        #: dropped from the account (it is no longer in loader.names())
        self._attach_allowed: dict[str, set[str] | None] = {}
        #: account -> services factory: (plugin_name) -> PluginServices
        self._services: dict[str, Callable[[str], Any]] = {}
        #: strong refs for fire-and-forget dispatch tasks (no GC mid-flight)
        self._pending: set[asyncio.Task] = set()
        #: account -> in-flight dispatch tasks (quiesced on deactivation)
        self._pending_by_account: dict[str, set[asyncio.Task]] = {}
        #: account -> dispatch tasks that have STARTED and not finished.
        #: The slow-handler pileup is what the flood bound caps; created-
        #: but-not-yet-scheduled tasks don't count — the bus dispatcher
        #: yields only every SLICE_EVENTS, so a same-slice burst of instant
        #: handlers would otherwise be miscounted as a flood and dropped.
        self._dispatch_started: dict[str, set[asyncio.Task]] = {}
        #: bounded grace for in-flight handlers during deactivate_account
        self._dispatch_grace_s = dispatch_grace_s
        #: central registry for plugin-attached dashboard UIs (Stage C);
        #: the dashboard routes dispatch through this in Stage D
        # offload_sync=True in the served stack: a slow synchronous
        # plugin callback must never freeze the dashboard's event loop
        # (bounded lane, see DashboardUIRegistry._invoke)
        #: serializes ACCOUNT-MAP REBUILDS (attach_account vs reload_plugin,
        #: review finding #8): an attach parked mid-build used to wholesale-
        #: assign its dict and silently discard a runtime reload had just
        #: activated — no hooks, no unregister. deactivate_account stays
        #: lock-free (attach calls it UNDER this lock).
        self._mutation_lock = asyncio.Lock()
        #: per-account activation-in-flight markers (set = idle): a stop/
        #: removal/deactivation landing during an in-flight activation
        #: WAITS for it to settle first — otherwise the activation's
        #: resumed hooks set active=True on a runtime the deactivator
        #: already walked past (an orphan nobody supervises)
        self._activation_events: dict[str, asyncio.Event] = {}
        #: activation EPOCHS: bumped when a teardown proceeds past an
        #: unsettled activation — the in-flight activation fences on it
        #: and undoes its commits instead of resurrecting state after
        #: teardown finished (review pass-3 #2)
        self._activation_epoch: dict[str, int] = {}
        #: bounded wait for in-flight activations (injectable for tests)
        self._activation_settle_timeout_s: float = 5.0
        #: bounded wait for an in-flight teardown before taking it over
        #: from an interrupted owner (injectable for tests)
        self._teardown_wait_timeout_s: float = 5.0
        self._ui_registry = ui_registry  # None when no dashboard UI system is present
        # the registry rebinds boot-time bound-method callbacks onto LIVE
        # plugin instances at dispatch; this manager is the resolver
        #: plugin ids whose UI registration was BOOT-owned (legacy —
        #: the dashboard UI system is removed; kept for interface compat)
        #: per-account cap on in-flight dispatch tasks (flood + slow
        #: plugins must never grow the live-task set without bound —
        #: the bus's own 20k bound is recreated one layer up otherwise)
        self._dispatch_limit = max(1, int(dispatch_inflight_limit))
        #: account -> dropped-dispatch counter (surfaced via
        #: :meth:`dispatch_stats; cleared when the account detaches)
        self._dispatch_dropped: dict[str, int] = {}
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
    def discover(self) -> dict[str, Any]:
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

    def manifests(self) -> list[dict[str, Any]]:
        """JSON-safe manifests (metadata + settings schema) for both UIs."""
        self._ensure_discovered()
        return self._loader.manifests()

    def names(self) -> list[str]:
        self._ensure_discovered()
        return self._loader.names()

    def _schema_for(self, plugin: str) -> tuple:
        """Setting schema of a plugin: from the loader record."""
        self._ensure_discovered()
        record = self._loader.record(plugin)  # PluginError when unknown
        return tuple(record.cls.settings)

    # -------------------------------------------------------------- accounts
    async def attach_account(self, account: str, allowed: set[str] | None = None) -> None:
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
        async with self._mutation_lock:
            return await self._attach_locked(account, allowed)

    async def _attach_locked(self, account: str, allowed: set[str] | None = None) -> None:
        # deactivate old runtimes (fire on_disable/on_unload on any that
        # were active — a restart after a failed start may have left them)
        if account in self._accounts:
            await self.deactivate_account(account)
        self._ensure_discovered()
        wanted = [n for n in self._loader.names() if allowed is None or n in allowed]
        runtimes: dict[str, PluginRuntime] = {}
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
            runtimes[name] = PluginRuntime(name=name, instance=instance, enabled=bool(enabled))
        # resolve settings NOW (not just at activate) so bad config fails
        # fast at attach — ported behavior from the audited original.
        # The account's bound-profile settings ride in as an extra layer
        # BETWEEN global and account overrides (defaults <- global <-
        # profile <- account) so the live settings view agrees with the
        # dashboard's effective-settings surface.
        profile_layers = await self._bound_profile_settings(account)
        for runtime in runtimes.values():
            try:
                runtime.settings = self._settings.resolve(
                    runtime.name,
                    self._schema_for(runtime.name),
                    account=account,
                    profile=profile_layers.get(runtime.name),
                )
            except Exception as exc:
                runtime.enabled = False
                runtime.last_error = f"settings: {type(exc).__name__}: {exc}"
                runtime.error_count += 1
                self._logger.error(
                    "plugin %s on %s failed settings resolution; disabled (%s)",
                    runtime.name,
                    account,
                    exc,
                )
        self._accounts[account] = runtimes
        self._attach_allowed[account] = None if allowed is None else set(allowed)
        self._logger.info(
            "attached %d plugin(s) to %s (allowed=%s)",
            len(runtimes),
            account,
            sorted(allowed) if allowed is not None else "all",
        )
        # self-command registry tracks the (assigned ∧ enabled) set — the
        # same hook covers assignment mutations (the account manager
        # re-attaches on every one) and plain (re)attaches
        self._sync_commands(account)

    async def reload_plugin(self, name: str) -> dict[str, Any]:
        """Hot-reload one plugin: validate → teardown → restore LIVE runtimes.

        Preserves per-account assignments and enabled flags. Contract
        (audit-hardened):
        - the re-import is VALIDATED FIRST — a broken plugin file must
          not unload the working copy (the loader restores the previous
          records when a scan fails);
        - teardown fires the real hooks (on_disable/on_unload) and
          quiesces in-flight dispatch for each affected account;
        - the restored runtime carries a LIVE instance, and accounts
          where the plugin was active are re-activated immediately —
          reload used to leave ``instance=None, active=False`` corpses
          that received zero events until an account restart.
        Returns a summary dict. Raises PluginError when the plugin is
        unknown or the re-import fails (the OLD state is preserved).
        """
        if name == "__rescan__":
            # special key: full re-scan without targeting one plugin
            self._loader.discover()
            found = self._loader.names()
            self._logger.info("plugin directory rescan: %d plugin(s)", len(found))
            return {"plugin": "__rescan__", "discovered": found, "restored_accounts": 0}
        # the account map is rebuilt under the same lock attach_account
        # holds — an in-flight attach's wholesale assignment would
        # otherwise discard what we restore here (no hooks, no cleanup)
        async with self._mutation_lock:
            return await self._reload_locked(name)

    async def _reload_locked(self, name: str) -> dict[str, Any]:
        # VALIDATE the fresh import BEFORE touching any live state: pop
        # the record and re-scan. A failing scan must leave BOTH the
        # live runtimes AND the loader record intact — the loader's own
        # rollback snapshot is taken AFTER this pop, so the record is
        # restored HERE (review finding #6: a failed re-scan used to
        # leave the plugin "unknown" for create()/names() while its
        # runtime kept running).
        old_record = self._loader._records.get(name)
        self._loader._records.pop(name, None)
        try:
            self._loader.discover()
        except BaseException:
            if old_record is not None:
                self._loader._records[name] = old_record
            raise  # nothing was torn down; the working copy is intact
        if name not in self._loader.names():
            if old_record is not None:
                self._loader._records[name] = old_record
            raise PluginError(f"unknown plugin {name!r}")
        # snapshot the affected accounts (runtime + liveness captured
        # BEFORE teardown mutates the runtime in place)
        affected = {}
        was_active = {}
        was_loaded = {}
        for acct, runtimes in self._accounts.items():
            rt = runtimes.get(name)
            if rt is not None:
                affected[acct] = rt
                was_active[acct] = rt.active
                was_loaded[acct] = rt.loaded
        # accounts that are ASSIGNED the plugin but hold no runtime for it
        # (it was unloaded, then a re-attach rebuilt the map without it)
        orphaned = [
            acct
            for acct, runtimes in self._accounts.items()
            if name not in runtimes
            and (
                self._attach_allowed.get(acct, set()) is None
                or name in (self._attach_allowed.get(acct) or ())
            )
        ]
        # TEARDOWN: the runtime is marked unavailable to NEW dispatch and
        # popped from the account map FIRST (review finding #1: events
        # admitted after the quiesce snapshot could still pass the
        # dispatch gates and run on the instance mid-unload); then the
        # in-flight tasks drain; then the hooks fire on captured state.
        for acct, rt in affected.items():
            await self._await_activation_settled(acct, timeout=self._activation_settle_timeout_s)
            # re-read AFTER the settle: an activation that was mid-hook at
            # snapshot time has finished by now, and teardown/re-activate
            # decisions on the stale flags skipped on_disable/on_unload
            was_active[acct] = rt.active
            was_loaded[acct] = rt.loaded
            rt.active = False  # blocks new dispatch admission NOW
            self._accounts[acct].pop(name, None)
            await self._quiesce_dispatch(acct)
            # ONE coordinated teardown path (exactly-once hooks even when
            # the late activation's compensation arrives mid-unload)
            await self._teardown_runtime(
                acct,
                rt,
                was_active=was_active.get(acct, False),
                was_loaded=was_loaded.get(acct, False),
            )
        # unregister the stale UI; the boot pass below re-registers it
        if self._ui_registry is not None:
            self._ui_registry.detach(name)
        # RESTORE with live instances; re-activate where it was running
        restored = 0
        reactivated = 0
        for acct, old_rt in list(affected.items()) + [(a, None) for a in orphaned]:
            runtimes = self._accounts.get(acct)
            if runtimes is None:
                continue  # account gone while we worked
            profile_layers = await self._bound_profile_settings(acct)
            try:
                instance = self._loader.create(name)
            except Exception:
                self._logger.exception(
                    "plugin %s failed to construct on %s after reload", name, acct
                )
                instance = None
            rt = PluginRuntime(
                name=name,
                instance=instance,
                enabled=old_rt.enabled if old_rt else True,
                loaded=False,
                active=False,
            )
            runtimes[name] = rt
            restored += 1
            # resolve settings NOW — same contract as attach_account
            # (fail-fast on bad config; the effective-settings view and
            # a stopped account's states() keep truthful values). The
            # re-activation below re-resolves for the live view anyway.
            try:
                resolved = self._settings.resolve(
                    name,
                    self._schema_for(name),
                    account=acct,
                    profile=profile_layers.get(name),
                )
                # keep the dict IDENTITY: the account's cached services
                # facade holds the previous runtime's dict, which must
                # stay a live view of the replacement's settings
                carried = getattr(old_rt, "settings", None)
                if isinstance(carried, dict):
                    carried.clear()
                    carried.update(resolved)
                    resolved = carried
                rt.settings = resolved
            except Exception as exc:
                rt.enabled = False
                rt.last_error = f"settings: {type(exc).__name__}: {exc}"
                rt.error_count += 1
                self._logger.error(
                    "plugin %s on %s failed settings resolution after reload; disabled (%s)",
                    name,
                    acct,
                    exc,
                )
            # REVALIDATE liveness, not the pre-teardown snapshot (review
            # finding #2): deactivate_account (account stop) pops the
            # services entry during our awaits — activating on a stopped
            # account would leave an unsupervised active plugin behind
            if (
                instance is not None
                and was_active.get(acct)
                and self._services.get(acct) is not None
            ):
                ok = False
                try:
                    await self._activate_one(acct, rt)
                    ok = True
                except Exception:
                    self._logger.exception(
                        "plugin %s failed to re-activate on %s after reload", name, acct
                    )
                if self._services.get(acct) is None:
                    # the account went down DURING activation — run the
                    # per-runtime teardown EXPLICITLY (raising into a
                    # logging-only except left the replacement active,
                    # hooks fired, on a stopped account — review pass-2 #2)
                    await self._teardown_runtime(acct, rt)
                elif ok:
                    reactivated += 1
            # the hub must reflect the REPLACEMENT's command declarations
            # even when it was not re-activated (review finding #13)
            self._sync_commands(acct)
        self._logger.info(
            "plugin %s reloaded (restored on %d account(s), re-activated on %d)",
            name,
            restored,
            reactivated,
        )
        return {"plugin": name, "restored_accounts": restored, "reactivated_accounts": reactivated}

    async def unload_plugin(self, name: str) -> dict[str, Any]:
        """Unload one plugin: tear down on every account, unregister UI.

        Same quiesce-then-hooks path as reload (new dispatch blocked,
        in-flight tasks drained, ``on_disable``/``on_unload`` fired
        exactly once), so nothing ``on_enable`` started leaks. Per-account
        settings and ASSIGNMENTS are preserved in the database — the
        runtime entries stay (inactive, no instance) and reload_plugin
        restores them. The plugin is removed from the discovered set and
        will not be offered again until discover() re-scans or
        reload_plugin is called.
        """
        if not self._discovered:
            self.discover()
        async with self._mutation_lock:
            detached = 0
            for acct, runtimes in list(self._accounts.items()):
                rt = runtimes.get(name)
                if rt is None:
                    continue
                await self._await_activation_settled(
                    acct, timeout=self._activation_settle_timeout_s
                )
                # read the flags AFTER the settle (a mid-hook activation
                # finishes there; stale flags skipped the unload hooks)
                was_active, was_loaded = rt.active, rt.loaded
                rt.active = False  # blocks new dispatch admission NOW
                await self._quiesce_dispatch(acct)
                await self._teardown_runtime(acct, rt, was_active=was_active, was_loaded=was_loaded)
                rt.loaded = False
                rt.active = False
                rt.instance = None
                self._sync_commands(acct)
                detached += 1
            if self._ui_registry is not None:
                self._ui_registry.detach(name)
            # remove from the loader so it's not re-offered
            self._loader._records.pop(name, None)
        self._logger.info("plugin %s unloaded (detached from %d account(s))", name, detached)
        return {"plugin": name, "detached_accounts": detached}

    def detach_account(self, account: str) -> None:
        """Drop the account's instances WITHOUT firing unload hooks.

        The account manager calls deactivate_account first when the plugins
        were active; detach only clears bookkeeping.
        """
        self._accounts.pop(account, None)
        self._attach_allowed.pop(account, None)
        self._services.pop(account, None)
        self._activation_events.pop(account, None)
        # NOTE: the EPOCH is deliberately NOT popped — it is a fence
        # marker for stragglers of a removed account; clearing it here
        # made a fenced late activation compare 0 == captured-0 and
        # resurrect its runtime after removal finished
        # dispatch bookkeeping: any task still queued dies at the services
        # gate in _dispatch (the account's services are gone)
        self._pending_by_account.pop(account, None)
        self._dispatch_started.pop(account, None)
        self._dispatch_dropped.pop(account, None)
        # the account's self-command surface is gone with it
        self.command_hub.forget(account)

    async def activate_account(self, account: str, services: Any) -> None:
        """Resolve settings + fire on_load/on_enable for enabled plugins.

        ``services`` is EITHER a PluginServices-like object OR a factory
        ``(plugin_name) -> services`` (duck-checked) — each plugin gets its
        own facade either way.
        """
        # serialize against a prior in-flight activation on this account
        await self._await_activation_settled(account, timeout=self._activation_settle_timeout_s)
        ev = self._activation_events.get(account)
        if ev is None:
            ev = asyncio.Event()
            ev.set()
            self._activation_events[account] = ev
        ev.clear()
        try:
            runtimes = self._accounts.get(account)
            if runtimes is None:
                raise PluginError(f"account {account!r} has no attached plugins (attach first)")
            factory = self._services_factory(services)
            self._services[account] = factory
            epoch = self._activation_epoch.get(account, 0)
            fenced = False
            # SNAPSHOT + identity recheck: a concurrent reload (or detach)
            # pops/replaces dict entries while hooks are parked —
            # iterating the live dict raised, and activating a detached
            # runtime would resurrect an invisible orphan
            for runtime in list(runtimes.values()):
                if self._accounts.get(account, {}).get(runtime.name) is not runtime:
                    continue  # replaced or removed while we were parked
                if not runtime.enabled:
                    continue
                await self._activate_one(account, runtime)
                if self._accounts.get(account, {}).get(runtime.name) is not runtime:
                    # a RELOAD (or detach) replaced this runtime while its
                    # hook was parked: the OLD instance just committed
                    # active=True but is no longer reachable through the
                    # account map — nothing would ever run its unload
                    # hooks, leaking whatever on_enable started. Tear it
                    # down directly, scoped to THIS runtime only (the
                    # account itself is still up — no account-wide fence).
                    await self._teardown_runtime(account, runtime)
                    continue
                if self._activation_epoch.get(account, 0) != epoch:
                    # FENCED: a teardown proceeded past this activation's
                    # settle timeout while its hook was parked — undo the
                    # commit so the account stays down, and STOP: the
                    # remaining runtimes must not run their hooks on a
                    # down account just to be torn down after
                    await self._teardown_runtime(account, runtime)
                    fenced = True  # noqa: F841 — signals the fence path was taken
                    break
        finally:
            ev.set()
        self._logger.info("activated plugins on %s", account)

    async def _activate_one(self, account: str, runtime: PluginRuntime) -> None:
        """Load + enable one runtime, isolating any hook failure."""
        if runtime.instance is None:
            # constructor failed at attach: nothing to drive. Disable (a
            # later set_enabled(True) retry must not leave an "enabled but
            # dead" runtime) and keep the original init error visible.
            runtime.enabled = False
            self._logger.error(
                "plugin %s on %s has no instance (constructor failed); cannot activate",
                runtime.name,
                account,
            )
            return
        # ANY activation attempt re-arms the teardown lifecycle (review
        # pass-8 #4): the unload marker must clear when the LOAD begins —
        # clearing only after on_enable meant a fresh on_load followed by
        # a failing on_enable left _unload_done set, and the later
        # deactivation skipped on_unload for the newly loaded resources
        runtime._disable_done = False
        runtime._unload_done = False
        services = self._services.get(account, lambda _n: None)(runtime.name)
        runtime.ctx = None  # PHASE-4 (S7-L1): new facade -> rebuild cache
        try:
            profile = (await self._bound_profile_settings(account)).get(runtime.name)
            resolved = self._settings.resolve(
                runtime.name,
                self._schema_for(runtime.name),
                account=account,
                profile=profile,
            )
        except Exception as exc:
            runtime.enabled = False
            runtime.last_error = f"settings: {type(exc).__name__}: {exc}"
            runtime.error_count += 1
            self._logger.error(
                "plugin %s on %s failed settings resolution; disabled (%s)",
                runtime.name,
                account,
                exc,
            )
            return
        # update IN PLACE, never rebind: the services facade (captured above)
        # and every context already hold THIS dict — a fresh object would
        # fork the documented live-settings view
        runtime.settings.clear()
        runtime.settings.update(resolved)
        # cache the context on the runtime (the PHASE-4 dispatch cache):
        # ui_stats/ui_panels read it too, and it stays None only until
        # the first dispatch otherwise
        ctx = runtime.ctx = self._context(
            account, runtime, services, explicit=self._explicit_keys(account, runtime.name)
        )
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

    async def _await_activation_settled(
        self, account: str, timeout: float = 5.0, *, fence: bool = False
    ) -> None:
        """Bounded wait for any in-flight activation on ``account``.

        Deactivation, removal and reload call this BEFORE touching the
        account's runtimes: an activation parked in on_load/on_enable
        would otherwise resume AFTER their teardown pass and leave an
        active, hook-fired runtime behind (review pass-2 #1).

        ``fence=True`` (deactivation/removal ONLY — the callers that
        actually tear the account down) arms the epoch undo on timeout.
        Reload and re-activation wait but never fence: their timing out
        must not unload plugins on an account that is still RUNNING
        (review pass-4 #4)."""
        ev = self._activation_events.get(account)
        if ev is None or ev.is_set():
            return
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if fence:
                # teardown proceeds now; the epoch bump makes the
                # still-running activation abort/undo its remaining
                # commits when its hook finally returns — a hook cannot
                # be cancelled (it runs in the caller's task), but it
                # must not resurrect state after teardown
                self._activation_epoch[account] = self._activation_epoch.get(account, 0) + 1
            self._logger.warning(
                "activation of %s did not settle within %.1fs; proceeding%s",
                account,
                timeout,
                " (the late activation will be fenced)" if fence else "",
            )

    def active_instance_for(self, plugin_name: str, account=None):
        """VALIDATED live instance for a plugin, optionally per account.

        Raises PluginError naming the exact problem (unknown account,
        plugin not assigned, disabled, not active) — the dashboard's
        account-context validation path. ``account=None`` returns the
        first active instance or None (no error).
        """
        if account is not None and account not in self._accounts:
            raise PluginError(f"unknown account {account!r}")
        if account is not None:
            runtime = self._accounts[account].get(plugin_name)
            if runtime is None:
                raise PluginError(f"plugin {plugin_name!r} is not assigned to account {account!r}")
            if not runtime.enabled:
                raise PluginError(f"plugin {plugin_name!r} is disabled on {account!r}")
            if not runtime.active or runtime.instance is None:
                raise PluginError(
                    f"plugin {plugin_name!r} on {account!r} has no active "
                    f"runtime — start the account"
                )
            return runtime.instance
        for runtimes in self._accounts.values():
            runtime = runtimes.get(plugin_name)
            if (
                runtime is not None
                and runtime.enabled
                and runtime.active
                and runtime.instance is not None
            ):
                return runtime.instance
        return None

    def services_for(self, account: str, plugin_name: str):
        """The account's services facade for one plugin (or None).

        None means the account has no active service wiring (never
        started) — dispatch refuses account-scoped actions in that
        state rather than fabricating a facade.
        """
        factory = self._services.get(account)
        if factory is None:
            return None
        return factory(plugin_name)

    def close(self) -> None:
        """Tear the manager down: bus subscriptions off, pending tasks reaped.

        Called by Framework.shutdown after accounts stopped — a lingering
        subscription set would keep dispatching to dead plugin runtimes.
        """
        for unsub in (
            self._unsubscribe,
            self._unsub_edited,
            self._unsub_deleted,
            self._unsub_reaction,
        ):
            with contextlib.suppress(Exception):
                unsub()
        for task in [t for t in list(self._pending) if not t.done()]:
            task.cancel()
        self._pending_by_account.clear()
        COMMAND_HUB.unbind(self)  # self-command invocations route nowhere now
        # the bootstrap registers THIS manager's AccessManager on the
        # process hub; leaving it there after the owning stack shut down
        # hands every later direct-construction stack (tests, nested
        # sims) a DEAD gate that fails every invocation closed — drop
        # the process reference when this manager owns it
        own_access = getattr(self, "_access_manager", None)
        if own_access is not None and getattr(self.command_hub, "access", None) is own_access:
            self.command_hub.access = None

    async def deactivate_account(self, account: str) -> None:
        """Fire on_disable + on_unload for the account's active plugins.

        The services entry is popped BEFORE the hook loop (the factory is
        captured locally): a ``set_enabled(account, ..., True)`` landing on
        an await point mid-deactivate must see ``account not in
        self._services`` and NOT fire activation hooks into an account that
        is going down (otherwise a plugin could be left ``active`` on a
        stopped account).
        """
        await self._await_activation_settled(
            account, timeout=self._activation_settle_timeout_s, fence=True
        )
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
        # SNAPSHOT: a concurrent reload pops/replaces dict entries while
        # hooks are parked (live-dict iteration raised) — and every
        # runtime unloads through the ONE coordinated path
        for runtime in list(runtimes.values()):
            await self._teardown_runtime(account, runtime, services_factory=factory)
        # UI lifecycle (merged contract): a stopped account ends its
        # RUNTIME association only — the plugin-wide UI DEFINITION always
        # survives deactivation (the page stays in the sidebar, usable
        # again on restart; runtime-bound actions simply refuse while no
        # account runs the plugin). This subsumes the boot-owned
        # exemption: boot AND live-owned definitions both persist.
        # Definitions detach ONLY at plugin boundaries: unload, reload
        # (validated replacement) or framework shutdown.

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
                "cancelled %d dispatch task(s) still running on %s after %.2fs grace",
                len(stragglers),
                account,
                self._dispatch_grace_s,
            )
            # bounded re-reap: a handler that swallows CancelledError
            # (bare except, shield, long finally) must not hang
            # deactivation forever — log survivors and move on
            _, undead = await asyncio.wait(stragglers, timeout=self._dispatch_grace_s)
            for task in undead:
                self._logger.warning(
                    "dispatch task on %s survived cancel; abandoning (task=%s)",
                    account,
                    getattr(task, "get_name", lambda task=task: repr(task))(),
                )

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
                # clear active BEFORE awaiting the hook (mirrors
                # deactivate_account's ordering): a concurrent
                # deactivate must not see an active runtime past this
                # point and fire on_disable a second time
                runtime.active = False
                services = self._services.get(account, lambda _n: None)(plugin)
                ctx = self._context(account, runtime, services)
                with contextlib.suppress(Exception):
                    await self._hook(
                        account, runtime, runtime.instance.on_disable, ctx, "on_disable"
                    )
        self._bus.publish_nowait(
            PluginEnabledChanged(account=account, plugin=plugin, enabled=runtime.enabled)
        )
        # enabled state is part of command visibility — re-publish
        self._sync_commands(account)

    async def set_setting(self, account: str, plugin: str, key: str, value: Any) -> Any:
        """Validate + persist + apply one plugin setting for one account."""
        runtime = self._runtime(account, plugin)
        schema = self._schema_for(plugin)
        validated = await self._settings.set_plugin_setting(account, plugin, key, value, schema)
        runtime.settings[key] = validated  # live view (contexts share the dict)
        return validated

    async def refresh_settings(self, account: str | None = None, plugin: str | None = None) -> int:
        """Re-resolve the LIVE settings of attached runtimes (in place).

        Profile bind/rebind/clear and profile edits/deletes change the
        profile layer of the merge, but resolution otherwise only runs at
        attach/activate/reload — a running plugin kept stale values while
        the effective-settings view showed the new ones. ``account`` /
        ``plugin`` narrow the scope (None = all). A runtime whose new
        merge fails validation keeps its previous values (logged).
        Returns the number of runtimes refreshed.
        """
        refreshed = 0
        for acct, runtimes in list(self._accounts.items()):
            if account is not None and acct != account:
                continue
            targets = [
                rt
                for name, rt in runtimes.items()
                if (plugin is None or name == plugin) and rt.instance is not None
            ]
            if not targets:
                continue
            profile_layers = await self._bound_profile_settings(acct)
            for rt in targets:
                try:
                    resolved = self._settings.resolve(
                        rt.name,
                        self._schema_for(rt.name),
                        account=acct,
                        profile=profile_layers.get(rt.name),
                    )
                except Exception as exc:
                    self._logger.error(
                        "settings refresh failed for %s on %s; keeping the previous values (%s)",
                        rt.name,
                        acct,
                        exc,
                    )
                    continue
                rt.settings.clear()  # in place: contexts share this dict
                rt.settings.update(resolved)
                refreshed += 1
        return refreshed

    async def clear_setting(self, account: str, plugin: str, key: str) -> Any:
        """Remove one account-layer override; live view falls back to the
        next layer (profile/global/plugin default) — mirrors
        :meth:`set_setting`. The bound profile layer is threaded through
        so the recomputed effective value respects the same precedence
        as attach/activate time (defaults <- global <- profile)."""
        runtime = self._runtime(account, plugin)
        schema = self._schema_for(plugin)
        profile = (await self._bound_profile_settings(account)).get(plugin)
        effective = await self._settings.clear_plugin_setting(
            account, plugin, key, schema, profile=profile
        )
        if schema:
            runtime.settings[key] = effective  # live view (shared dict)
        else:
            runtime.settings.pop(key, None)
        return effective

    # ---------------------------------------------------------------- state
    def _ui_ctx(self, account: str, entry: PluginRuntime) -> Any:
        """The runtime's cached dispatch context for UI providers.

        Mirrors ``_dispatch``'s lazy rebuild (the PHASE-4 cache): built
        from the manager's own state, never the plugin-side ``_ctx``
        convention. Returns None when no services facade can be resolved
        (account going down) — callers skip, they do not feed None in.
        """
        ctx = entry.ctx
        if ctx is None or ctx.services is None:
            services = self._services.get(account, lambda _n: None)(entry.name)
            ctx = entry.ctx = self._context(account, entry, services)
        return ctx if ctx.services is not None else None

    async def _teardown_runtime(
        self,
        account: str,
        runtime: PluginRuntime,
        *,
        was_active: bool | None = None,
        was_loaded: bool | None = None,
        services_factory: Any = _UNSET_FACTORY,
    ) -> None:
        """Fire one runtime's unload hooks and reset its flags — EXACTLY
        ONCE per runtime, coordinated.

        Every teardown path (deactivate, reload, the late-activation
        compensation) routes through here. A per-runtime event serializes
        concurrent attempts: a second teardown arriving while the first
        is still awaiting an unload hook WAITS (bounded) and then leaves
        — firing on_unload twice ran non-idempotent plugin cleanup
        concurrently (review pass-6).

        ``was_active``/``was_loaded`` default to the runtime's CURRENT
        flags; callers that mutated flags earlier (reload blocks
        dispatch by flipping ``active`` before draining) pass their
        captured snapshot instead. ``services_factory`` defaults to the
        account's live entry; deactivate passes the factory it already
        popped (its hooks must still see the facades)."""
        if was_active is None:
            was_active = runtime.active
        if was_loaded is None:
            was_loaded = runtime.loaded
        if services_factory is _UNSET_FACTORY:
            services_factory = self._services.get(account)
        # ATOMIC CLAIM loop: exactly ONE caller ever owns a teardown
        # attempt at a time. Ownership = an unset event + the owning
        # task, assigned with no await between the check and the claim
        # (single event loop ⇒ atomic). Everything else waits.
        waited = False
        while True:
            ev = getattr(runtime, "_teardown_event", None)
            owner = getattr(runtime, "_teardown_task", None)
            abandoned = ev is not None and not ev.is_set() and owner is not None and owner.done()
            if ev is None or ev.is_set() or owner is None or owner.done():
                # idle, completed-and-idle, or an ABANDONED attempt —
                # claim it. When taking over an abandoned attempt, MERGE
                # its owed-hook record into ours: the interruptor already
                # cleared the lifecycle flags, so this caller's fresh
                # snapshot alone would SKIP the unfinished cleanup
                # (review pass-8 #1). A completed attempt's stale record
                # is never merged (its hooks already ran).
                if abandoned:
                    owe_a, owe_l = getattr(runtime, "_teardown_owe", (False, False))
                    was_active = bool(was_active) or owe_a
                    was_loaded = bool(was_loaded) or owe_l
                    self._logger.warning(
                        "taking over the abandoned teardown of %s on %s", runtime.name, account
                    )
                ev = runtime._teardown_event = asyncio.Event()
                runtime._teardown_task = asyncio.current_task()
                runtime._teardown_owe = (bool(was_active), bool(was_loaded))
                break
            # a LIVE attempt is in flight — wait bounded for its outcome
            if waited:
                return  # bounded patience: the live owner owns the hooks
            waited = True
            try:
                await asyncio.wait_for(ev.wait(), timeout=self._teardown_wait_timeout_s)
            except asyncio.TimeoutError:
                self._logger.warning(
                    "teardown of %s on %s did not settle within %.1fs",
                    runtime.name,
                    account,
                    self._teardown_wait_timeout_s,
                )
            if ev.is_set():
                # that attempt COMPLETED — the hooks ran exactly once
                return
            # unset event + done owner = abandoned — loop once more; the
            # claim branch above performs the merge and the atomic claim
        # — we own the attempt from here —
        try:
            services = services_factory(runtime.name) if callable(services_factory) else None
            ctx = self._context(account, runtime, services)
            runtime.ctx = None
            # per-hook done markers: a takeover re-runs ONLY the hooks
            # the interrupted owner never finished (markers are set
            # right after each await returns — cancellation cannot land
            # between the await completing and the marker assignment)
            if (
                was_active
                and runtime.enabled
                and runtime.instance is not None
                and not getattr(runtime, "_disable_done", False)
            ):
                # recorded; still unload
                with contextlib.suppress(Exception):
                    await self._hook(
                        account, runtime, runtime.instance.on_disable, ctx, "on_disable"
                    )
                runtime._disable_done = True
            if (
                was_loaded
                and runtime.instance is not None
                and not getattr(runtime, "_unload_done", False)
            ):
                with contextlib.suppress(Exception):
                    await self._hook(account, runtime, runtime.instance.on_unload, ctx, "on_unload")
                runtime._unload_done = True
                self._bus.publish_nowait(PluginUnloaded(account=account, plugin=runtime.name))
            runtime.active = False
            runtime.loaded = False
        except BaseException:
            # INTERRUPTED (cancellation, KI/SystemExit, or an unexpected
            # error outside the hook guards): reset the lifecycle flags
            # so nothing downstream sees an active runtime, and leave
            # the owed-hook record (minus what completed) with the
            # event UNSET and the owning task DONE — the abandoned
            # state the claim loop takes over
            runtime.active = False
            runtime.loaded = False
            owe_a, owe_l = runtime._teardown_owe
            runtime._teardown_owe = (
                owe_a and not getattr(runtime, "_disable_done", False),
                owe_l and not getattr(runtime, "_unload_done", False),
            )
            raise
        else:
            # COMPLETED: signal every waiter (the else clause runs only
            # when no exception escaped the body)
            ev.set()

    def states(self, account: str) -> list[dict[str, Any]]:
        """Per-account plugin state for both UIs (sorted by name)."""
        runtimes = self._accounts.get(account, {})
        return [
            {
                "name": rt.name,
                "enabled": rt.enabled,
                "loaded": rt.loaded,
                # "active" = hooks fired + dispatch-eligible (a loaded
                # runtime on a stopped account is NOT active) — the
                # Plugins page renders this distinction
                "active": rt.active,
                "settings": dict(rt.settings),
                "last_error": rt.last_error,
                "error_count": rt.error_count,
            }
            for rt in sorted(runtimes.values(), key=lambda r: r.name)
        ]

    def dispatch_stats(self, account: str) -> dict[str, int]:
        """Bounded-dispatch health for one account: how many dispatch
        tasks are unfinished (created or running), how many are actually
        executing handlers, and how many events were dropped because the
        per-account flood bound (:meth:`_on_message`) was full."""
        queued = self._pending_by_account.get(account, ())
        started = self._dispatch_started.get(account, ())
        return {
            "in_flight": sum(1 for t in queued if not t.done()),
            "running": sum(1 for t in started if not t.done()),
            "dropped": self._dispatch_dropped.get(account, 0),
        }

    def allowed_names(self, account: str) -> list[str]:
        """Names currently attached to the account (for error messages)."""
        return sorted(self._accounts.get(account, {}))

    def live_settings(self, account: str, plugin: str) -> dict[str, Any] | None:
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
    def command_entries(self, account: str) -> list[tuple[CommandSpec, str]]:
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
        out: list[tuple[CommandSpec, str]] = []
        taken: dict[str, str] = {}
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
                        declared.name,
                        plugin_name,
                        taken[declared.name],
                    )
                    continue
                taken[declared.name] = plugin_name
                spec = replace(
                    declared,
                    handler=self._bind_handler(declared.handler, runtime.instance),
                    category=declared.category or plugin_name,
                )
                out.append((spec, plugin_name))
        return out

    def commands_for(self, account: str) -> list[CommandSpec]:
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
        args: list[str],
        channel_id: int,
        *,
        guild_id: int | None = None,
        invoker_id: int | None = None,
        raw: str = "",
    ) -> None:
        """Run one self-command and reply in-channel through the pipeline.

        Fully error-contained (a crashing handler must never kill the
        account or its Bot): the handler's string return value, its usage
        errors (``ValueError`` — including ``SettingsValidationError``)
        and the internal-error fallback are ALL sent back through the
        per-plugin services facade — the same paced pipeline plugin sends
        use — never a raw client send.

        The command gate mirrors the trigger-word path exactly: the
        invoking Discord user id must pass AccessManager.can_invoke —
        the manager's own ``_access_manager`` (bootstrap seam) first,
        the hub's bootstrap-registered AccessManager second, and the
        credentials view's owner ids (``services.credentials``) only as
        the legacy last resort for direct-construction stacks with no
        access manager anywhere. Non-owners and UNKNOWN invokers
        (``invoker_id=None``) are ignored with no reply — can_invoke
        fails closed.

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
                "self-command %r on %s dropped: %s",
                name,
                account,
                "plugins not activated" if factory is None else "no services facade",
            )
            return
        # THE command gate: AccessManager.can_invoke (per-account
        # permission list + admin bypass, fail-closed — an unknown/None
        # invoker DENIES). The manager's own ``_access_manager``
        # (bootstrap seam) wins; when it is absent the hub's
        # bootstrap-registered AccessManager backs it up (covers
        # framework verbs like estop on a deactivated account, where
        # there are no facade credentials to read either). The
        # credentials view's owner ids are the LEGACY last resort for
        # direct-construction stacks (simulator/tests) with NO access
        # manager anywhere.
        access = getattr(self, "_access_manager", None)
        if access is None:
            access = getattr(self.command_hub, "access", None)
        allowed = False
        if access is not None:
            try:
                allowed = await access.can_invoke(account, invoker_id)
            except Exception:
                self._logger.exception(
                    "access check failed for %r on %s — failing closed", invoker_id, account
                )
                allowed = False
        else:
            owners = getattr(getattr(services, "credentials", None), "owner_user_ids", ()) or ()
            allowed = invoker_id in set(owners)
        if not allowed:
            self._logger.info(
                "self-command %r on %s ignored: invoker %s not allowed", name, account, invoker_id
            )
            return
        ctx = CommandContext(
            account=account,
            plugin=plugin,
            services=services,
            logger=logging.getLogger(f"libzent.{account}.{plugin}.command"),
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
                ctx.parsed.update(parse_command_args(spec, list(args or [])))
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

    async def _send_reply(
        self, services: Any, channel_id: int, text: str, logger: logging.Logger
    ) -> None:
        """One paced reply (NORMAL priority — interactive, single message)."""
        if len(text) > MAX_REPLY_CHARS:
            text = text[: MAX_REPLY_CHARS - 20] + "\n… (truncated)"
        if services is None:
            # framework verb (estop!) on an account with no active plugin
            # pipeline: the reply CANNOT be delivered in-band — log it at
            # ERROR with the full text so the operator's answer survives
            # (the retired remote control's undeliverable-reply contract)
            logger.error(
                "self-command reply undeliverable (no services facade) — reply was: %s", text
            )
            return
        try:
            await services.send_message(int(channel_id), text, priority=Priority.NORMAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            # the reply is best-effort exactly like the remote-control
            # bridge: log it loudly so the operator's answer survives
            logger.error("self-command reply undeliverable — reply was: %s", text)

    def _find_command(self, account: str, name: str) -> RegisteredCommand | None:
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

    async def _bound_profile_settings(self, account: str) -> dict[str, dict[str, Any]]:
        """Per-plugin bound-profile settings for one account:
        ``{plugin: {key: value}}`` with the reserved ``channels`` scope
        stripped (it rides along separately in the effective views).

        Read through the plugin repository's owning database (the same
        duck-typed reach :class:`_ManagerSettingsApi` uses); stacks
        without profile storage degrade to an empty map — resolution
        falls back to the plain defaults <- global <- account merge
        exactly as before. Dangling bindings (deleted profile) are
        skipped, matching the API layer's parity rule.
        """
        profile_repo, binding_repo = _ManagerSettingsApi._repos(self)
        reader = getattr(binding_repo, "bindings", None)
        if not callable(reader):
            return {}
        try:
            bindings = dict(await reader(account) or {})
        except Exception:
            return {}
        getter = getattr(profile_repo, "get", None)
        if not callable(getter):
            return {}
        reserved = plugin_scopes.CHANNELS_KEY
        out: dict[str, dict[str, Any]] = {}
        for plugin, profile_name in bindings.items():
            if profile_name is None:
                continue
            try:
                record = await getter(plugin, profile_name)
            except Exception:
                continue
            if record is None:
                continue  # dangling binding: skip (parity with api.py)
            settings = getattr(record, "settings", None) or {}
            layer = {k: v for k, v in settings.items() if k != reserved}
            if layer:
                out[plugin] = layer
        return out

    # -------------------------------------------------------------- routing
    def _on_message(self, event: Event) -> None:
        """Bus handler (sync): fan a MessageReceived out as a fire-and-forget
        dispatch task so the publisher is never blocked by a slow plugin."""
        if not isinstance(event, (MessageReceived, MessageEdited, MessageDeleted, ReactionAdded)):
            return
        runtimes = self._accounts.get(event.account)
        if not runtimes:
            return
        # FLOOD BOUND: cap the per-account dispatch workload in two tiers.
        # Without this, a burst of events against slow plugins grows the
        # live-task set without bound (the bus's own 20k queue bound is
        # recreated one layer up otherwise).
        #   tier 1 — STARTED-and-unfinished tasks >= limit: the slow-handler
        #            pileup proper; the NEW event is dropped (drop-newest:
        #            in-flight work is never killed mid-flight).
        #   tier 2 — unfinished backlog >= 4x limit: admission safety net
        #            for zero-yield drain slices (created tasks that have
        #            not run yet); still bounded, still drops-newest.
        # Instant handlers must NEVER lose events to a burst: they only
        # exist momentarily in tier 2 and age out as soon as the loop
        # schedules them. The WARNING is rate-limited (first drop + every
        # 50th) so a sustained flood cannot flood the logs too.
        account = event.account
        running = self._dispatch_started.get(account)
        n_running = sum(1 for t in running if not t.done()) if running is not None else 0
        queued = self._pending_by_account.get(account)
        n_backlog = sum(1 for t in queued if not t.done()) if queued is not None else 0
        if n_running >= self._dispatch_limit or n_backlog >= 4 * self._dispatch_limit:
            dropped = self._dispatch_dropped.get(account, 0) + 1
            self._dispatch_dropped[account] = dropped
            if dropped == 1 or dropped % 50 == 0:
                self._logger.warning(
                    "dispatch queue full for %s (%d running, %d backlog, "
                    "cap %d) — event dropped (%d dropped so far)",
                    account,
                    n_running,
                    n_backlog,
                    self._dispatch_limit,
                    dropped,
                )
            return
        task = asyncio.get_running_loop().create_task(self._dispatch(event, runtimes))
        self._pending.add(task)
        self._pending_by_account.setdefault(account, set()).add(task)
        task.add_done_callback(lambda t, account=account: self._reap_dispatch(account, t))

    def _reap_dispatch(self, account: str, task: asyncio.Task) -> None:
        """Drop a finished dispatch task from all bookkeeping sets."""
        self._pending.discard(task)
        tasks = self._pending_by_account.get(account)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._pending_by_account.pop(account, None)
        started = self._dispatch_started.get(account)
        if started is not None:
            started.discard(task)
            if not started:
                self._dispatch_started.pop(account, None)

    async def _dispatch(self, event: MessageReceived, runtimes: dict[str, PluginRuntime]) -> None:
        """Await every enabled plugin's handle_event, isolated per plugin.

        RUNNING bookkeeping: the task registers itself at coroutine entry
        so the flood bound's tier-1 count reflects handlers actually
        executing (a created-but-unscheduled task must not be mistaken
        for a parked handler). Removal happens in _reap_dispatch.

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
        current = asyncio.current_task()
        if current is not None:
            self._dispatch_started.setdefault(event.account, set()).add(current)
        if self._accounts.get(event.account) is not runtimes:
            return
        factory = self._services.get(event.account)
        if factory is None:
            return
        for runtime in list(
            runtimes.values()
        ):  # snapshot: reload mutates the live dict across awaits
            if not runtime.enabled or not runtime.active or runtime.instance is None:
                continue
            # PHASE-4 (S7-L1): the context (logger lookup + dataclass) is
            # cached per runtime — all its inputs are stable per activation
            # (settings stays the LIVE dict, services the same facade), and
            # rebuilding it per event × per plugin sat on the hottest path.
            # (round-3: a ctx rebuilt with no services mid-deactivate is
            # SKIPPED, never delivered — the plugin would crash on None)
            ctx = runtime.ctx
            if ctx is None or ctx.services is None:
                services = self._services.get(event.account, lambda _n: None)(runtime.name)
                ctx = runtime.ctx = self._context(event.account, runtime, services)
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
                    runtime.name,
                    type(event).__name__,
                    event.account,
                )

    # ------------------------------------------------------------ internals
    def _explicit_keys(self, account: str, plugin: str) -> frozenset[str] | None:
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
        explicit.update(layers.get("accounts", {}).get(account, {}).get(plugin, {}))
        return frozenset(explicit)

    def _runtime(self, account: str, plugin: str) -> PluginRuntime:
        runtimes = self._accounts.get(account)
        if runtimes is None or plugin not in runtimes:
            known = self.allowed_names(account) or self.names()
            raise PluginError(f"account {account!r} has no plugin {plugin!r} (known: {known})")
        return runtimes[plugin]

    async def _hook(
        self, account: str, runtime: PluginRuntime, fn: Any, ctx: Any, hook: str
    ) -> None:
        """Await one lifecycle hook; record + re-raise failures on the runtime."""
        try:
            await fn(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.error_count += 1
            runtime.last_error = f"{hook}: {type(exc).__name__}: {exc}"
            self._logger.exception("%s failed for %s:%s", hook, account, runtime.name)
            raise

    @staticmethod
    def _services_factory(services: Any) -> Callable[[str], Any]:
        """Accept a services object OR a (plugin_name) -> services factory."""
        if callable(services) and not hasattr(services, "send_message"):
            return services
        return lambda _name: services

    def _context(
        self,
        account: str,
        runtime: PluginRuntime,
        services: Any,
        explicit: frozenset | None = None,
    ) -> Any:
        from core.plugin_base import PluginContext

        return PluginContext(
            account=account,
            plugin=runtime.name,
            settings=runtime.settings,
            services=services,
            logger=logging.getLogger(f"libzent.{account}.{runtime.name}"),
            explicit_settings=explicit,
        )

    async def invoke_handler(
        self, account: str, plugin: str, handler: str, payload: dict[str, Any]
    ) -> Any:
        """Invoke one plugin handler on one account (W4 chain runtime's
        production invoker).

        The handler runs with the SAME per-(account, plugin) context the
        lifecycle hooks see — its sends ride the paced pipeline, its
        settings are the live view. Raises PluginError when the plugin
        is not attached to the account (the caller records a visible
        skip); handler exceptions propagate (the caller records FAILED)."""
        rt = self._runtime(account, plugin)
        if not getattr(rt, "active", False):
            raise PluginError(f"plugin {plugin!r} is not active on account {account!r}")
        handler_fn = getattr(rt.instance, handler, None)
        if handler_fn is None or not callable(handler_fn):
            raise PluginError(
                f"plugin {plugin!r} has no callable handler {handler!r} on account {account!r}"
            )
        services = self._services.get(account, lambda _n: None)(plugin)
        ctx = self._context(account, rt, services)
        return await handler_fn(ctx, payload)


class _ManagerSettingsApi:
    """CommandSettingsApi over the plugin manager's own surfaces.

    ``set``/``clear`` go through :meth:`PluginManager.set_setting` /
    ``clear_setting` — the SAME validation path the ControlAPI's
    ``plugins_set_setting`` uses (parity by construction). ``effective``
    reproduces the Phase-1 account view (``accounts_effective_settings``)
    through the managers, never HTTP: plugin Setting defaults <- the
    global settings layer <- the bound profile's settings (minus the
    reserved ``channels`` key) <- the account's per-plugin overrides,
    with the profile's channel scope riding along under ``"channels"``.

    Profile/binding repositories are reached duck-typed through the
    plugin repository's owning database (``BaseRepository._db`` — the
    documented back-reference every repository holds); stacks without
    profile storage degrade to the settings-manager view with
    ``channels = None``.
    """

    def __init__(self, manager: PluginManager, account: str, plugin: str) -> None:
        self._manager = manager
        self._account = account
        self._plugin = plugin

    # ------------------------------------------------------------- repos
    @staticmethod
    def _repos(manager: PluginManager) -> tuple:
        """(plugin_profiles repo, account_plugins repo) — either None."""
        database = getattr(manager._repo, "_db", None)  # BaseRepository
        return (
            getattr(database, "plugin_profiles", None),
            getattr(database, "account_plugins", None),
        )

    # -------------------------------------------------------------- api
    def schema(self) -> tuple:
        return tuple(self._manager._schema_for(self._plugin))

    async def binding(self) -> str | None:
        _profile_repo, binding_repo = self._repos(self._manager)
        reader = getattr(binding_repo, "bindings", None)
        if not callable(reader):
            return None
        try:
            return (await reader(self._account) or {}).get(self._plugin)
        except Exception:
            return None

    async def profiles(self) -> list[dict[str, Any]]:
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

    async def effective(self) -> dict[str, Any]:
        """Fully merged effective settings for the (account, plugin).

        Merge order (highest wins): plugin Setting defaults <- the
        global settings layer <- the bound profile's settings (minus
        the reserved ``channels`` key) <- the account's per-plugin
        overrides, with the profile's channel scope riding along under
        ``"channels"``. Same order :meth:`SettingsManager.resolve`
        enforces at attach/activate time, so every surface agrees (the
        Phase-1 API view merges defaults <- profile <- account without
        the global layer — reconciliation is the API owner's task;
        this manager-side view is the four-layer one).

        A display surface: unknown keys are skipped and values that fail
        validation fall back to the raw stored value (parity with how
        the API layer renders), instead of failing the read.
        """
        schema = self.schema()
        defs = {s.name: s for s in schema}
        merged: dict[str, Any] = {s.name: s.validate(s.default) for s in schema}
        reserved = plugin_scopes.CHANNELS_KEY

        def _apply(layer: Any) -> None:
            for key, value in (layer or {}).items():
                if key == reserved or key not in defs:
                    continue
                try:
                    merged[key] = defs[key].validate(value)
                except SettingsValidationError:
                    merged[key] = value  # parity: the API layer applies raw

        # global file layer (lowest of the three override layers)
        with contextlib.suppress(Exception):
            _apply(self._manager._settings.layers_view().get("global", {}).get(self._plugin, {}))
        # bound profile layer
        profile_name: str | None = None
        scope: dict[str, Any] | None = None
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
                _apply(getattr(record, "settings", None))
                raw_scope = (getattr(record, "settings", None) or {}).get(reserved)
                try:
                    scope = plugin_scopes.validate(raw_scope) if raw_scope is not None else None
                except ValueError:
                    scope = None
        # account overrides (highest); the scope is profile-owned
        try:
            layers = self._manager._settings.layers_view()
            overrides = layers.get("accounts", {}).get(self._account, {}).get(self._plugin, {})
        except Exception:
            overrides = {}
        _apply(overrides)
        merged[reserved] = scope
        return merged

    async def set(self, key: str, value: Any) -> Any:
        """Same validation path as /api/plugins/set-setting."""
        return await self._manager.set_setting(self._account, self._plugin, key, value)

    async def clear(self, key: str) -> Any:
        return await self._manager.clear_setting(self._account, self._plugin, key)


__all__ = ["PluginManager", "PluginRuntime"]
