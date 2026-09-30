"""Account container: owns every account session and dispatches events to plugins.

Responsibilities:
  * instantiate plugin instances per account (one instance per pair),
  * start/stop accounts individually or all at once,
  * route bus events to the owning account's enabled plugins (error-isolated),
  * expose helpers plugins use: collect_messages, wait_for, broadcast (sync groups).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional

from .account import AccountConfig, AccountSession, AccountStatus, ReconnectPolicy
from .credentials import Credentials
from .events import Event, EventBus, MessageEvent
from .plugin_base import PluginBase, PluginContext
from .plugin_loader import PluginLoader
from .rate_limiter import RateLimiterService
from .scheduler import TaskScheduler
from .send_engine import SendEngine, SendPolicy
from .settings import SettingsStore

LOG = logging.getLogger("slaughtermudae.container")


class ContainerError(RuntimeError):
    """Raised for invalid container operations (unknown account, double start...)."""


@dataclass
class PluginRuntime:
    """One plugin instance bound to one account."""

    name: str
    instance: PluginBase
    enabled: bool = True
    loaded: bool = False
    settings: Dict[str, Any] = field(default_factory=dict)
    # ERROR AUDIT FIX: runtime crashes are now visible in status/dashboard
    last_error: Optional[str] = None
    error_count: int = 0


@dataclass
class AccountRuntime:
    """Everything the container tracks for one account."""

    config: AccountConfig
    plugins: Dict[str, PluginRuntime] = field(default_factory=dict)
    session: Optional[AccountSession] = None
    #: set by stop_account; tells the supervisor to stand down
    stop_requested: bool = False
    supervisor: Optional[asyncio.Task] = None
    #: serialized send lane (FarmLite prefixrate engine); None = bypass
    engine: Optional[SendEngine] = None
    #: ERROR AUDIT FIX: why the last START failed (factory errors happen
    #: before a session exists, so session.last_error can't hold them)
    last_error: Optional[str] = None

    def status_dict(self) -> Dict[str, Any]:
        if self.session is not None:
            status = self.session.status.value
        else:
            # ERROR AUDIT FIX: a failed START (before any session existed)
            # reports 'failed', not a misleading 'created'
            status = AccountStatus.FAILED.value if self.last_error else AccountStatus.CREATED.value
        return {
            "name": self.config.name,
            "status": status,
            "user_id": self.session.user_id if self.session else None,
            "last_error": (self.session.last_error if self.session else None) or self.last_error,
            # serialized send lane state (FarmLite prefixrate engine)
            "engine": self.engine.stats() if self.engine else {"enabled": False},
            "sync_group": self.config.sync_group,
            # AUDIT FIX: the dashboard role chips read this key
            "roles": list(self.config.roles),
            "plugins": {
                name: {
                    "enabled": rt.enabled,
                    "loaded": rt.loaded,
                    "last_error": rt.last_error,
                    "error_count": rt.error_count,
                }
                for name, rt in sorted(self.plugins.items())
            },
        }


class AccountContainer:
    """Central multi-account runtime."""

    def __init__(
        self,
        *,
        credentials: Credentials,
        settings: SettingsStore,
        limiter: RateLimiterService,
        loader: PluginLoader,
        bus: Optional[EventBus] = None,
        client_factory: Optional[Callable[[AccountConfig, Credentials], Any]] = None,
        logger: Optional[logging.Logger] = None,
        reconnect_policy: Optional[ReconnectPolicy] = None,
        state: Optional[Any] = None,
        roles: Optional[Any] = None,
        audit: Optional[Any] = None,
        send_policy: Optional[SendPolicy] = None,
    ) -> None:
        self._credentials = credentials
        self._settings = settings
        self._limiter = limiter
        self._loader = loader
        self._bus = bus or EventBus()
        self._client_factory = client_factory
        self._logger = logger or LOG
        self._reconnect_policy = reconnect_policy or ReconnectPolicy()
        #: core.database.Database — persistent state source
        self._state = state
        #: core.roles.RoleRegistry — capability grants (None = no restriction)
        self._roles = roles
        #: core.audit.AuditRecorder — interaction trail (None = disabled)
        self._audit = audit
        #: core.send_engine.SendPolicy — serialized per-account lanes (FarmLite)
        self._send_policy = send_policy or SendPolicy()
        # REAL-TIME: per-account caches + REST header managers
        from .discord_cache import CacheManager

        self.cache_manager = CacheManager()  # public: ControlAPI reads snapshots
        self._rest_managers: Dict[str, Any] = {}
        # Task Scheduler: fires jobs through the full safety stack (lane +
        # limiter + cache validation), failures contained per job
        self.scheduler = TaskScheduler(
            database=state, send=self._scheduled_send,
            logger=logging.getLogger("slaughtermudae.scheduler"),
        )
        self._accounts: Dict[str, AccountRuntime] = {}
        self._background: set = set()  # strong refs for fire-and-forget tasks
        self._bus.subscribe(Event, self._dispatch)

    # ------------------------------------------------------------ properties
    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def limiter(self) -> RateLimiterService:
        return self._limiter

    def account_names(self) -> List[str]:
        return sorted(self._accounts)

    def sync_groups(self) -> Dict[str, List[str]]:
        """group name -> sorted member account names."""
        groups: Dict[str, List[str]] = {}
        for runtime in self._accounts.values():
            if runtime.config.sync_group:
                groups.setdefault(runtime.config.sync_group, []).append(runtime.config.name)
        return {g: sorted(members) for g, members in groups.items()}

    def status(self) -> List[Dict[str, Any]]:
        """JSON-safe status of every account."""
        return [self._accounts[name].status_dict() for name in sorted(self._accounts)]

    def _spawn_engine(self, runtime: AccountRuntime) -> Optional[SendEngine]:
        """Create + start the account's serialized send lane when enabled.

        Policy precedence: runtime-tuned DB value (kv 'engine:<account>') >
        account config 'send_engine' block > container default SendPolicy.
        """
        import json as _json

        policy = (
            SendPolicy.from_dict(runtime.config.send_engine)
            if runtime.config.send_engine is not None
            else self._send_policy
        )
        if self._state is not None:
            stored = self._state.kv_get(f"engine:{runtime.config.name}")
            if stored:
                try:
                    policy = SendPolicy.from_dict(_json.loads(stored))
                except (ValueError, _json.JSONDecodeError):
                    self._logger.warning("ignoring corrupt engine policy for %s", runtime.config.name)
        runtime.engine = None
        if not policy.enabled:
            return None
        engine = SendEngine(runtime.config.name, prefixrate=policy.prefixrate).start()
        runtime.engine = engine
        self._logger.info(
            "send engine for %s: prefixrate=%.2fs", runtime.config.name, policy.prefixrate
        )
        return engine

    async def _shutdown_engine(self, runtime: AccountRuntime) -> None:
        if runtime.engine is not None:
            await runtime.engine.stop()
            runtime.engine = None

    # -------------------------------------------------------------- accounts
    def add_account(self, config: AccountConfig) -> AccountRuntime:
        """Register an account; instantiates (but does not enable) its plugins.

        Resolves each plugin's settings immediately so bad config fails fast.
        """
        if config.name in self._accounts:
            raise ContainerError(f"account {config.name!r} already registered")
        runtime = AccountRuntime(config=config)
        wanted = config.plugins if config.plugins is not None else self._loader.names()
        wanted = list(dict.fromkeys(wanted))  # AUDIT FIX: dedupe, keep order
        # roles gate the plugin set: an account WITH roles only keeps the
        # plugins its roles allow (union); no roles => untouched (back-compat)
        if self._roles is not None and config.roles:
            allowed = self._roles.allowed_plugins(config.roles)
            if allowed is not None:
                wanted = [name for name in wanted if name in allowed]
        for name in wanted:
            instance = self._loader.create(name)
            resolved = self._settings.resolve(name, type(instance).settings, account=config.name)
            enabled = True
            if self._state is not None:
                enabled = self._state.plugin_enabled(config.name, name, default=True)
            runtime.plugins[name] = PluginRuntime(
                name=name, instance=instance, enabled=enabled, settings=resolved
            )
        self._accounts[config.name] = runtime
        self._logger.info(
            "registered account %s (plugins=%s roles=%s)",
            config.name, sorted(runtime.plugins), config.roles or "-",
        )
        return runtime

    def remove_account(self, name: str) -> None:
        """Unload plugins and drop a stopped account.

        AUDIT FIX: the RUNNING refusal now happens BEFORE any state mutation,
        so a failed removal no longer leaves the account unsupervised.
        """
        runtime = self._get(name)
        if runtime.session and runtime.session.status in (AccountStatus.RUNNING, AccountStatus.RECONNECTING):
            raise ContainerError(f"stop account {name!r} before removing it")
        runtime.stop_requested = True
        if runtime.supervisor is not None:
            runtime.supervisor.cancel()
            runtime.supervisor = None
        for prt in runtime.plugins.values():
            if prt.loaded:
                self._spawn_background(self._safe_unload(runtime, prt))
        del self._accounts[name]

    async def _safe_unload(self, runtime: AccountRuntime, prt: PluginRuntime) -> None:
        try:
            await prt.instance.on_unload(self._context(runtime, prt))
        except Exception:
            self._logger.exception("on_unload failed for %s:%s", runtime.config.name, prt.name)

    def _spawn_background(self, coro) -> None:
        """AUDIT FIX: keep strong refs to fire-and-forget tasks."""
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def start_account(self, name: str) -> AccountRuntime:
        """Connect one account and enable its plugins (on_load + on_enable)."""
        runtime = self._get(name)
        runtime.stop_requested = False
        if runtime.session and runtime.session.status == AccountStatus.RUNNING:
            raise ContainerError(f"account {name!r} is already running")
        if self._client_factory is None:
            raise ContainerError("no client_factory configured (simulation or live required)")
        try:
            client = self._client_factory(runtime.config, self._credentials)
        except Exception as exc:
            runtime.last_error = f"start failed: {type(exc).__name__}: {exc}"
            self._logger.exception("client factory failed for %s", name)
            raise
        session = AccountSession(
            runtime.config, client, self._bus, self._limiter,
            logging.getLogger(f"slaughtermudae.account.{name}"),
            audit=self._audit,
            engine=self._spawn_engine(runtime),
            cache=self.cache_manager.cache_for(name),
            rest_manager=self._rest_manager_for(runtime),
            rest_gate=self._rest_gate_for(runtime),
        )
        runtime.session = session  # assigned FIRST so failures surface in status()
        await session.start()
        # REAL-TIME: ingest the client's cache (guilds/channels/DMs) — zero fetches
        self.cache_manager.cache_for(name).ingest_snapshot(session.cached_snapshot())
        for prt in runtime.plugins.values():
            if prt.enabled:
                await self._activate(runtime, prt)
        policy = (
            ReconnectPolicy.from_dict(runtime.config.reconnect)
            if runtime.config.reconnect is not None
            else self._reconnect_policy
        )
        if policy.enabled:
            runtime.supervisor = asyncio.create_task(
                self._supervise(runtime, policy), name=f"supervise-{name}"
            )
        return runtime

    def start_scheduler(self) -> None:
        """Start the interval job loop (safe to call repeatedly)."""
        self.scheduler.start()

    async def stop_scheduler(self) -> None:
        await self.scheduler.stop()

    async def _scheduled_send(self, account: str, channel_id: int, command: str) -> Any:
        """Scheduler hook: run a job's send through the FULL safety stack."""
        runtime = self._accounts.get(account)
        if runtime is None or runtime.session is None:
            raise ContainerError(
                f"job targets account {account!r} which is not running (skipping)"
            )
        return await runtime.session.send_message(int(channel_id), command)

    def _rest_manager_for(self, runtime: AccountRuntime):
        """Per-account REST header manager (Discord's own rate-limit state)."""
        from .rest_buckets import RestBucketManager

        manager = self._rest_managers.get(runtime.config.name)
        if manager is None:
            manager = RestBucketManager(
                runtime.config.name,
                on_global_cooldown=self._on_rest_global_cooldown,
            )
            self._rest_managers[runtime.config.name] = manager
        return manager

    def _rest_gate_for(self, runtime: AccountRuntime):
        """Single-flight REST gate: one HTTP call at a time, others wait."""
        from .rest_buckets import RestGate

        return RestGate(self._rest_manager_for(runtime))

    def _on_rest_global_cooldown(self, seconds: float) -> None:
        """A GLOBAL 429 raises EVERY account's lane spacing for the cooldown."""
        for runtime in self._accounts.values():
            if runtime.engine is not None and runtime.engine.prefixrate < seconds:
                runtime.engine.set_prefixrate(seconds)
                self._logger.warning(
                    "global REST cooldown: %s lane spacing raised to %.1fs",
                    runtime.config.name, seconds,
                )

    async def stop_account(self, name: str) -> AccountRuntime:
        """Disable plugins (on_disable) and disconnect one account.

        AUDIT FIX: idempotent — hooks fire once (skipped when the session is
        already STOPPED), and a caller's CancelledError now propagates instead
        of being swallowed with the supervisor grace timeout.
        """
        runtime = self._get(name)
        runtime.stop_requested = True
        supervisor = runtime.supervisor
        if supervisor is not None:
            try:
                await asyncio.wait_for(asyncio.shield(supervisor), timeout=1.0)
            except asyncio.TimeoutError:
                supervisor.cancel()
            except asyncio.CancelledError:
                raise
            runtime.supervisor = None
        already_stopped = runtime.session is not None and runtime.session.status == AccountStatus.STOPPED
        if not already_stopped:
            for prt in runtime.plugins.values():
                if prt.enabled and prt.loaded:
                    try:
                        await prt.instance.on_disable(self._context(runtime, prt))
                    except Exception:
                        self._logger.exception("on_disable failed for %s:%s", name, prt.name)
        if runtime.session is not None:
            await runtime.session.stop()
        await self._shutdown_engine(runtime)
        return runtime

    async def start_all(self) -> None:
        """Start every registered account; collects errors into logs."""
        self.start_scheduler()
        for name in self.account_names():
            try:
                await self.start_account(name)
            except Exception:
                self._logger.exception("failed to start account %s", name)

    async def stop_all(self) -> None:
        """Graceful shutdown: stop all accounts (used on CTRL-C / 'quit')."""
        await self.stop_scheduler()
        for name in self.account_names():
            try:
                await self.stop_account(name)
            except Exception:
                self._logger.exception("failed to stop account %s", name)

    # ---------------------------------------------------------------- plugins
    async def enable_plugin(self, account: str, plugin: str, enabled: bool) -> None:
        """Toggle a plugin at runtime, firing the lifecycle hooks."""
        runtime = self._get(account)
        prt = runtime.plugins.get(plugin)
        if prt is None:
            raise ContainerError(f"account {account!r} has no plugin {plugin!r}")
        if enabled and not prt.enabled:
            if self._state is not None:
                # ERROR AUDIT FIX: persist BEFORE mutating — a DB failure no
                # longer leaves memory and the database disagreeing
                self._state.set_plugin_enabled(account, plugin, True)
            prt.enabled = True
            if runtime.session and runtime.session.status == AccountStatus.RUNNING:
                await self._activate(runtime, prt)
        elif not enabled and prt.enabled:
            if self._state is not None:
                self._state.set_plugin_enabled(account, plugin, False)
            prt.enabled = False
            if prt.loaded:
                try:
                    await prt.instance.on_disable(self._context(runtime, prt))
                except Exception:
                    self._logger.exception("on_disable failed for %s:%s", account, plugin)

    async def _activate(self, runtime: AccountRuntime, prt: PluginRuntime) -> None:
        ctx = self._context(runtime, prt)
        try:
            if not prt.loaded:
                await prt.instance.on_load(ctx)
                prt.loaded = True
            await prt.instance.on_enable(ctx)
        except Exception:
            prt.enabled = False
            self._logger.exception(
                "plugin %s failed to activate on %s; disabled", prt.name, runtime.config.name
            )

    # ---------------------------------------------------------------- helpers
    def set_plugin_setting(self, account: str, plugin: str, key: str, value: Any) -> Any:
        """Validate + apply one plugin setting override for an account.

        Raises ContainerError for unknown account/plugin/setting and
        SettingValidationError for bad values (both surfaces share this path).
        Effect is immediate for future reads (ctx.settings is the same dict).
        """
        runtime = self._get(account)
        prt = runtime.plugins.get(plugin)
        if prt is None:
            raise ContainerError(f"account {account!r} has no plugin {plugin!r}")
        defs = {d.key: d for d in type(prt.instance).settings}
        if key not in defs:
            raise ContainerError(
                f"plugin {plugin!r} has no setting {key!r} (supports: {sorted(defs)})"
            )
        validated = defs[key].validate(value)
        prt.settings[key] = validated
        if self._state is not None:
            self._state.set_setting_override(account, plugin, key, validated)
        self._logger.info("setting %s:%s.%s = %r", account, plugin, key, validated)
        return validated

    def assign_role(self, account: str, role: str, assigned: bool) -> List[str]:
        """Grant/revoke a role on an account at runtime (persisted by caller).

        Role changes shape the plugin set on the NEXT start of the account —
        deliberate and predictable, no mid-run plugin yanking.
        """
        runtime = self._get(account)
        if assigned and role not in runtime.config.roles:
            runtime.config.roles.append(role)
        elif not assigned and role in runtime.config.roles:
            runtime.config.roles.remove(role)
        return list(runtime.config.roles)

    async def collect_messages(
        self, account: str, check: Callable[[MessageEvent], bool], timeout: float
    ) -> List[MessageEvent]:
        """Collect matching messages until `timeout` passes with no new match.

        Idle-timeout semantics mirror the original Soulcopy DM reader.
        ERROR AUDIT FIX: timeout must be positive (0/negative silently
        returned [] before).
        """
        if timeout <= 0:
            raise ValueError(f"collect_messages timeout must be > 0, got {timeout}")
        queue: asyncio.Queue[MessageEvent] = asyncio.Queue()

        def _handler(event: Event) -> None:
            if isinstance(event, MessageEvent) and event.account == account and check(event):
                queue.put_nowait(event)

        unsubscribe = self._bus.subscribe(MessageEvent, _handler)
        collected: List[MessageEvent] = []
        try:
            while True:
                collected.append(await asyncio.wait_for(queue.get(), timeout=timeout))
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            # ERROR AUDIT FIX: a raising check() previously vanished into the
            # bus logger (hex function address) and looked like "no messages"
            raise ContainerError(
                f"collect_messages failed for account {account!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            unsubscribe()
        return collected

    async def wait_for(
        self, account: str, check: Callable[[MessageEvent], bool], timeout: float
    ) -> Optional[MessageEvent]:
        """Wait for ONE matching message; None on timeout.

        AUDIT FIX: returns as soon as the FIRST match arrives (previously it
        sat through the full collect_messages idle window).
        """
        if timeout <= 0:
            raise ValueError(f"wait_for timeout must be > 0, got {timeout}")
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[MessageEvent]" = loop.create_future()

        def _handler(event: Event) -> None:
            if isinstance(event, MessageEvent) and event.account == account and check(event):
                if not future.done():
                    future.set_result(event)

        unsubscribe = self._bus.subscribe(MessageEvent, _handler)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            unsubscribe()

    async def broadcast(
        self, group: str, operation: Callable[[AccountSession], Coroutine[Any, Any, Any]]
    ) -> Dict[str, Any]:
        """Run `operation(session)` concurrently on every RUNNING group member.

        Sync groups in action: same operation, every account, results gathered.
        Returns: account -> result (or exception rendered as string).
        """
        members = self.sync_groups().get(group, [])
        running = [
            self._accounts[name].session
            for name in members
            if self._accounts[name].session
            and self._accounts[name].session.status == AccountStatus.RUNNING
        ]
        results = await asyncio.gather(
            *(operation(session) for session in running), return_exceptions=True
        )
        return {
            session.name: (repr(result) if isinstance(result, BaseException) else result)
            for session, result in zip(running, results)
        }

    # -------------------------------------------------------------- internals
    async def _supervise(self, runtime: AccountRuntime, policy: ReconnectPolicy) -> None:
        """Watch one running account; reconnect it with backoff when dropped.

        Only reacts to session.lost (unexpected drops). Deliberate stops set
        stop_requested and the supervisor exits without reconnecting.
        """
        name = runtime.config.name
        try:
            while not runtime.stop_requested:
                session = runtime.session
                if session is None:
                    return
                # AUDIT FIX: react to `lost` regardless of exact status so
                # drops during CONNECTING/reattach also trigger recovery
                if session.status == AccountStatus.RECONNECTING or session.lost.is_set():
                    await self._reconnect_loop(runtime, policy)
                    continue
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            return

    async def _reconnect_loop(self, runtime: AccountRuntime, policy: ReconnectPolicy) -> None:
        """Retry connecting a dropped account with exponential backoff."""
        name = runtime.config.name
        session = runtime.session
        if session is None:
            return
        attempt = 0
        while not runtime.stop_requested:
            delay = policy.delay_for(attempt)
            self._logger.warning(
                "account %s reconnect attempt %d in %.2fs", name, attempt + 1, delay
            )
            await asyncio.sleep(delay)
            if runtime.stop_requested:
                return
            try:
                client = self._client_factory(runtime.config, self._credentials)
                await session.reattach(client)
                return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # ERROR AUDIT FIX: SystemExit/KI must not
                attempt += 1          # kill the process silently mid-reattach
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    session.status = AccountStatus.FAILED
                    session.last_error = f"reconnect interrupted: {exc}"
                    self._logger.error("reconnect for %s interrupted: %r", name, exc)
                    return
                self._logger.warning("reconnect failed for %s: %s", name, exc)

    def _get(self, name: str) -> AccountRuntime:
        try:
            return self._accounts[name]
        except KeyError as exc:
            raise ContainerError(
                f"unknown account {name!r} (registered: {sorted(self._accounts)})"
            ) from exc
        except TypeError as exc:  # unhashable payloads via the dashboard
            raise ContainerError(f"account name must be a string, got {name!r}") from exc

    def _context(self, runtime: AccountRuntime, prt: PluginRuntime) -> PluginContext:
        session = runtime.session
        if session is None:  # lifecycle hooks before/after a session exists
            session = _NullSession(runtime.config.name)
        return PluginContext(
            account=runtime.config.name,
            plugin=prt.name,
            settings=prt.settings,
            session=session,
            limiter=self._limiter,
            credentials=self._credentials,
            container=self,
            logger=logging.getLogger(f"slaughtermudae.{runtime.config.name}.{prt.name}"),
        )

    async def _dispatch(self, event: Event) -> None:
        """Bus handler: fan an event out to the owning account's enabled plugins."""
        runtime = self._accounts.get(event.account)
        if runtime is None:
            return
        if self._audit is not None and isinstance(event, MessageEvent):
            preview = event.content.replace("\n", "\\n")[:80]
            self._audit.record(
                account=event.account, direction="in", action="event:message",
                channel_id=event.channel_id, content=preview,
                note=f"author={event.author_id}{' dm' if event.dm else ''}",
            )
        for prt in runtime.plugins.values():
            if not prt.enabled:
                continue
            try:
                await prt.instance.handle_event(self._context(runtime, prt), event)
            except asyncio.CancelledError:
                raise
            except Exception:
                prt.error_count += 1  # ERROR AUDIT FIX: visible in status/dashboard
                prt.last_error = f"{event.name}: {self._current_exception()}"
                self._logger.exception(
                    "plugin %s crashed handling %s on %s", prt.name, event.name, event.account
                )

    @staticmethod
    def _current_exception() -> str:
        import sys

        exc = sys.exc_info()[1]
        return f"{type(exc).__name__}: {exc}" if exc else "unknown error"


class _NullSession:
    """Stand-in used only for lifecycle hooks outside a live session."""

    def __init__(self, account: str) -> None:
        self.name = account
        self.user_id = None

    async def send_message(self, *args: Any, **kwargs: Any) -> None:
        raise ContainerError(f"account {self.name!r} has no active session")

    async def send_dm(self, *args: Any, **kwargs: Any) -> None:
        raise ContainerError(f"account {self.name!r} has no active session")
