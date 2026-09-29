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

from .account import AccountConfig, AccountSession, AccountStatus
from .credentials import Credentials
from .events import Event, EventBus, MessageEvent
from .plugin_base import PluginBase, PluginContext
from .plugin_loader import PluginLoader
from .rate_limiter import RateLimiterService
from .settings import SettingsStore

LOG = logging.getLogger("mudae.container")


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


@dataclass
class AccountRuntime:
    """Everything the container tracks for one account."""

    config: AccountConfig
    plugins: Dict[str, PluginRuntime] = field(default_factory=dict)
    session: Optional[AccountSession] = None

    def status_dict(self) -> Dict[str, Any]:
        return {
            "name": self.config.name,
            "status": self.session.status.value if self.session else AccountStatus.CREATED.value,
            "user_id": self.session.user_id if self.session else None,
            "sync_group": self.config.sync_group,
            "plugins": {
                name: {"enabled": rt.enabled, "loaded": rt.loaded}
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
    ) -> None:
        self._credentials = credentials
        self._settings = settings
        self._limiter = limiter
        self._loader = loader
        self._bus = bus or EventBus()
        self._client_factory = client_factory
        self._logger = logger or LOG
        self._accounts: Dict[str, AccountRuntime] = {}
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

    # -------------------------------------------------------------- accounts
    def add_account(self, config: AccountConfig) -> AccountRuntime:
        """Register an account; instantiates (but does not enable) its plugins.

        Resolves each plugin's settings immediately so bad config fails fast.
        """
        if config.name in self._accounts:
            raise ContainerError(f"account {config.name!r} already registered")
        runtime = AccountRuntime(config=config)
        wanted = config.plugins if config.plugins is not None else self._loader.names()
        for name in wanted:
            instance = self._loader.create(name)
            resolved = self._settings.resolve(name, type(instance).settings, account=config.name)
            runtime.plugins[name] = PluginRuntime(
                name=name, instance=instance, enabled=True, settings=resolved
            )
        self._accounts[config.name] = runtime
        self._logger.info("registered account %s (plugins=%s)", config.name, sorted(runtime.plugins))
        return runtime

    def remove_account(self, name: str) -> None:
        """Unload plugins and drop a stopped account."""
        runtime = self._get(name)
        if runtime.session and runtime.session.status == AccountStatus.RUNNING:
            raise ContainerError(f"stop account {name!r} before removing it")
        for prt in runtime.plugins.values():
            if prt.loaded:
                asyncio.get_running_loop().create_task(
                    prt.instance.on_unload(self._context(runtime, prt))
                )
        del self._accounts[name]

    async def start_account(self, name: str) -> AccountRuntime:
        """Connect one account and enable its plugins (on_load + on_enable)."""
        runtime = self._get(name)
        if runtime.session and runtime.session.status == AccountStatus.RUNNING:
            raise ContainerError(f"account {name!r} is already running")
        if self._client_factory is None:
            raise ContainerError("no client_factory configured (simulation or live required)")
        client = self._client_factory(runtime.config, self._credentials)
        session = AccountSession(
            runtime.config, client, self._bus, self._limiter,
            logging.getLogger(f"mudae.account.{name}"),
        )
        await session.start()
        runtime.session = session
        for prt in runtime.plugins.values():
            if prt.enabled:
                await self._activate(runtime, prt)
        return runtime

    async def stop_account(self, name: str) -> AccountRuntime:
        """Disable plugins (on_disable) and disconnect one account."""
        runtime = self._get(name)
        for prt in runtime.plugins.values():
            if prt.enabled and prt.loaded:
                try:
                    await prt.instance.on_disable(self._context(runtime, prt))
                except Exception:
                    self._logger.exception("on_disable failed for %s:%s", name, prt.name)
        if runtime.session is not None:
            await runtime.session.stop()
        return runtime

    async def start_all(self) -> None:
        """Start every registered account; collects errors into logs."""
        for name in self.account_names():
            try:
                await self.start_account(name)
            except Exception:
                self._logger.exception("failed to start account %s", name)

    async def stop_all(self) -> None:
        """Graceful shutdown: stop all accounts (used on CTRL-C / 'quit')."""
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
            prt.enabled = True
            if runtime.session and runtime.session.status == AccountStatus.RUNNING:
                await self._activate(runtime, prt)
        elif not enabled and prt.enabled:
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
    async def collect_messages(
        self, account: str, check: Callable[[MessageEvent], bool], timeout: float
    ) -> List[MessageEvent]:
        """Collect matching messages until `timeout` passes with no new match.

        Idle-timeout semantics mirror the original Soulcopy DM reader.
        """
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
        finally:
            unsubscribe()
        return collected

    async def wait_for(
        self, account: str, check: Callable[[MessageEvent], bool], timeout: float
    ) -> Optional[MessageEvent]:
        """Wait for ONE matching message; None on timeout."""
        found = await self.collect_messages(account, check, timeout)
        return found[0] if found else None

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
    def _get(self, name: str) -> AccountRuntime:
        try:
            return self._accounts[name]
        except KeyError as exc:
            raise ContainerError(f"unknown account {name!r}") from exc

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
            logger=logging.getLogger(f"mudae.{runtime.config.name}.{prt.name}"),
        )

    async def _dispatch(self, event: Event) -> None:
        """Bus handler: fan an event out to the owning account's enabled plugins."""
        runtime = self._accounts.get(event.account)
        if runtime is None:
            return
        for prt in runtime.plugins.values():
            if not prt.enabled:
                continue
            try:
                await prt.instance.handle_event(self._context(runtime, prt), event)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception(
                    "plugin %s crashed handling %s on %s", prt.name, event.name, event.account
                )


class _NullSession:
    """Stand-in used only for lifecycle hooks outside a live session."""

    def __init__(self, account: str) -> None:
        self.name = account
        self.user_id = None

    async def send_message(self, *args: Any, **kwargs: Any) -> None:
        raise ContainerError(f"account {self.name!r} has no active session")

    async def send_dm(self, *args: Any, **kwargs: Any) -> None:
        raise ContainerError(f"account {self.name!r} has no active session")
