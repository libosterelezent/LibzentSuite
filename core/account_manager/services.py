"""PluginServices implementation for the account manager (A6).

Implements the :class:`core.plugin_base.PluginServices` protocol by duck
typing (no import of ``core.plugin_manager`` — that package is built in
parallel and is never needed here at runtime).

DESIGN — how sends await task completion (the lead asked for this):
    ``send_message``/``send_dm`` submit a task into the scheduler and then
    AWAIT its completion through a shared :class:`TaskAckRegistry`:

      * the AccountManager builds ONE registry per process and hands it to
        every services instance (the registry subscribes ONCE to
        ``TaskCompleted`` / ``TaskFailed`` / ``TaskCancelled`` on the bus),
      * ``send_*`` registers a future under ``task.id`` and waits with a
        bounded timeout (``task_ack_timeout``, default 30s),
      * ``TaskCompleted`` resolves the future with the event's summary,
        ``TaskFailed``/``TaskCancelled`` raise :class:`core.errors.SchedulerError`
        carrying the reason, a timeout raises ``SchedulerError`` too. A
        terminal failure whose ``error_kind`` names a FlowBlocked subclass
        or a transient transport error (``ConnectionError`` /
        ``TimeoutError``) is re-raised as THAT exception type across the
        seam — the flow-abort contract and transient handling survive the
        ack boundary,

    ``scheduler.submit`` is synchronous, so the future is registered in the
    same event-loop step as the submit — a completion event can never arrive
    between the two (the bus only dispatches across an ``await``).

    RETURN VALUE CONTRACT (documented deviation): the pinned protocol
    declares ``send_* -> DiscordMessage`` but the pipeline only produces a
    human summary string (the DiscordMessage never round-trips the bus).
    ``send_*`` therefore returns ``None`` on success and RAISES on failure —
    plugins treat the calls as fire-and-await-ack. Callers wanting the
    message shape can use ``scheduler.get(task_id)`` / history.
"""
from __future__ import annotations

import asyncio
import re
import logging
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from ..errors import (
    AccessDenied,
    AccountError,
    CacheInvalid,
    FetchRequired,
    FlowBlocked,
    PluginError,
    SchedulerError,
    TargetGone,
)
from ..event_bus import (
    Event,
    EventBus,
    MessageReceived,
    TaskCancelled,
    TaskCompleted,
    TaskFailed,
)
from ..priority import Priority
from .cache import DiscordCache, resolve_dm_channel

LOG = logging.getLogger("libzent.services")


class TaskAckRegistry:
    """task_id -> future map fed by the bus Task* events (one subscription)."""

    def __init__(self, bus: EventBus, scheduler: Any = None) -> None:
        """Subscribe the three completion events on ``bus`` (idempotent per bus).

        ``scheduler`` (optional) enables best-effort cancellation when a
        waiter times out — a plugin must never see a failure for a send
        that still executes later.
        """
        self._bus = bus
        self._scheduler = scheduler
        self._futures: Dict[int, "asyncio.Future[Any]"] = {}
        bus.subscribe(TaskCompleted, self._on_completed)
        bus.subscribe(TaskFailed, self._on_failed)
        bus.subscribe(TaskCancelled, self._on_cancelled)

    # ------------------------------------------------------------- handlers
    def _on_completed(self, event: TaskCompleted) -> None:
        future = self._futures.pop(event.task_id, None)
        if future is not None and not future.done():
            future.set_result(event.summary)

    #: FlowBlocked family, reconstructed across the task-ack seam so the
    #: plugin-side catch (the flow-abort contract) actually fires
    _FLOW_CLASSES = {
        "FlowBlocked": FlowBlocked,
        "CacheInvalid": CacheInvalid,
        "TargetGone": TargetGone,
        "AccessDenied": AccessDenied,
        "FetchRequired": FetchRequired,
    }

    #: Transient transport errors reconstructed as their BUILTIN types. The
    #: scheduler retries ConnectionError/TimeoutError internally; once those
    #: retries are exhausted the terminal TaskFailed carries the class name
    #: in ``error_kind`` — without this map the waiting plugin would get an
    #: opaque SchedulerError and flow code would have to substring-match
    #: English error text to guess transience (fragile, round-3 finding).
    #: Constructed with a single message string (not the (account, reason)
    #: FlowBlocked shape). ``ServerRateLimit`` stays UNMAPPED: the scheduler
    #: always retries it at least once, so it stays retried-invisible here.
    _TRANSIENT_CLASSES = {
        "ConnectionError": ConnectionError,
        "TimeoutError": TimeoutError,
        # stdlib subclasses: reconstructed as their own type so plugins
        # catching the base still match (isinstance holds for all of these)
        "ConnectionResetError": ConnectionResetError,
        "ConnectionAbortedError": ConnectionAbortedError,
        "ConnectionRefusedError": ConnectionRefusedError,
        "BrokenPipeError": BrokenPipeError,
    }

    def _on_failed(self, event: TaskFailed) -> None:
        future = self._futures.pop(event.task_id, None)
        if future is None or future.done():
            return
        kind = getattr(event, "error_kind", "") or ""
        blocked = self._FLOW_CLASSES.get(kind)
        if blocked is not None:
            future.set_exception(
                blocked(event.account, self._flow_reason(event.error))
            )
            return
        transient = self._TRANSIENT_CLASSES.get(kind)
        if transient is not None:
            future.set_exception(transient(self._flow_reason(event.error)))
            return
        future.set_exception(
            SchedulerError(f"task {event.task_id} failed: {event.error}")
        )

    @staticmethod
    def _flow_reason(error: str) -> str:
        """'"TargetGone: [alpha] flow blocked: channel 500 vanished"' -> reason."""
        text = error or ""
        # drop the leading "ClassName: " (the scheduler's error format)
        if ": " in text:
            text = text.split(": ", 1)[1]
        # drop the "[account] flow blocked: " wrapper FlowBlocked adds
        match = re.match(r"^\[[^\]]*\] flow blocked: (.*)$", text, flags=re.DOTALL)
        return match.group(1) if match else text

    def _on_cancelled(self, event: TaskCancelled) -> None:
        future = self._futures.pop(event.task_id, None)
        if future is not None and not future.done():
            future.set_exception(
                SchedulerError(f"task {event.task_id} was cancelled")
            )

    # --------------------------------------------------------------- await
    def register(self, task_id: int) -> "asyncio.Future[Any]":
        """Create + register the future for ``task_id`` (loop-safe).

        Raises SchedulerError when a LIVE future already exists for the
        id: silently replacing it would orphan the first waiter (it hangs
        for its full timeout and then cancels a task another waiter is
        consuming).
        """
        existing = self._futures.get(task_id)
        if existing is not None and not existing.done():
            raise SchedulerError(
                f"task {task_id} is already being awaited"
            )
        future: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
        self._futures[task_id] = future
        return future

    async def wait_for_task(self, task_id: int, timeout: float) -> Any:
        """Await one task's completion event; returns the TaskCompleted summary.

        Raises:
            ConnectionError / TimeoutError: the task failed terminally with
            that transient transport error (typed across the ack seam — the
            scheduler already exhausted its retries).
            SchedulerError: the task failed with anything else, was
            cancelled, or did not
            produce a completion event within ``timeout`` seconds (in the
            timeout case the task is CANCELLED first — a plugin must never
            observe a failure for a send that still executes minutes later).
        """
        future = self.register(task_id)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.CancelledError:
            # the CALLER was cancelled (not a timeout): drop the entry or
            # it lingers until a terminal event that may never come
            self._futures.pop(task_id, None)
            raise
        except asyncio.TimeoutError:
            self._futures.pop(task_id, None)
            cancel = getattr(self._scheduler, "cancel", None)
            if callable(cancel):  # best-effort: queued tasks die now instead
                try:  # of delivering a surprise duplicate later
                    cancel(task_id)
                except Exception:
                    pass
            raise SchedulerError(
                f"task {task_id} did not complete within {timeout:.0f}s "
                "(cancelled)"
            ) from None

    def pending(self) -> List[int]:
        """Task ids currently awaited (introspection/tests)."""
        return sorted(self._futures)


class MessageWaiterRegistry:
    """Per-account waiter index (FINAL-PHASE F1-b′).

    Replaces the old unfiltered per-waiter bus subscriptions — where M
    concurrent waiters each evaluated EVERY message on EVERY account
    (M×E handler invocations, measured 75% waste at 4 accounts) — with
    ONE broadcast subscription dispatching only to the owning account's
    waiters. A waiter for account A structurally never runs for B's
    events (the account key IS the routing), preserving the isolation
    invariant while cutting waiter fan-out from M to M/A per event.

    One instance per AccountManager (mirroring TaskAckRegistry); the
    per-plugin facades receive it and route their wait_for_message /
    collect_messages handlers through it. Handlers are invoked sync, in
    registration order, each isolated (a raising waiter sets its own
    future's exception; the registry never propagates).
    """

    def __init__(self, bus: EventBus) -> None:
        self._by_account: Dict[str, List[Callable[[MessageReceived], None]]] = {}
        self._unsub = bus.subscribe(MessageReceived, self._on_message)

    def _on_message(self, event: Event) -> None:
        if not isinstance(event, MessageReceived):
            return
        handlers = self._by_account.get(event.account)
        if not handlers:
            return
        for handler in list(handlers):
            try:
                handler(event)
            except Exception:  # isolation: the waiter owns its failure
                LOG.debug("message waiter raised", exc_info=True)

    def add(self, account: str,
            handler: Callable[[MessageReceived], None]) -> Callable[[], None]:
        """Register one waiter; returns the idempotent unsubscribe."""
        self._by_account.setdefault(account, []).append(handler)

        def _remove() -> None:
            try:
                self._by_account.get(account, []).remove(handler)
            except ValueError:
                pass
        return _remove

    def forget_account(self, account: str) -> None:
        """Removal cascade: a re-created same-named account starts clean."""
        self._by_account.pop(account, None)

    def close(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def pending(self) -> Dict[str, int]:
        return {name: len(hs) for name, hs in self._by_account.items()}


class AccountPluginServices:
    """Per-(account, plugin) facade implementing the PluginServices protocol.

    Built by :meth:`core.account_manager.AccountManager.services_for`. Two
    optional keyword arguments extend the pinned constructor (defaults keep
    the pinned signature fully usable):

      * ``ack_registry`` — shared TaskAckRegistry (the manager passes its
        own so the bus is subscribed exactly once),
      * ``client_getter`` — zero-arg callable returning the account's LIVE
        client (or None); needed for cache-miss DM opens,
      * ``cooldowns`` — the framework's shared CooldownService; enables the
        account+plugin-scoped ``cooldown_*`` plugin API below,
      * ``plugin_state`` — the framework's shared PluginStateService;
        enables the pair-scoped ``state_*`` plugin API below.
    """

    def __init__(
        self,
        account: str,
        plugin: str,
        *,
        bus: EventBus,
        scheduler: Any,
        credentials: Any,
        cache: DiscordCache,
        settings_dict: Optional[Dict[str, Any]] = None,
        ack_registry: Optional[TaskAckRegistry] = None,
        waiter_registry: Optional[MessageWaiterRegistry] = None,
        client_getter: Optional[Callable[[], Any]] = None,
        api_cache_repo: Any = None,
        cooldowns: Any = None,
        plugin_state: Any = None,
    ) -> None:
        self._account = account
        self._plugin = plugin
        self._bus = bus
        self._scheduler = scheduler
        self._credentials = credentials
        self._cache = cache
        #: live settings view — the plugin manager refreshes this dict IN PLACE
        self._settings_dict: Dict[str, Any] = (
            settings_dict if isinstance(settings_dict, dict) else {}
        )
        self._ack_registry = ack_registry
        self._waiter_registry = waiter_registry
        self._client_getter = client_getter
        #: per-account persistent entity cache (database.cache repository)
        self._api_cache = api_cache_repo
        #: shared cooldown service (None = cooldown API unavailable here)
        self._cooldowns = cooldowns
        #: shared plugin-state service (None = state API unavailable here)
        self._plugin_state = plugin_state
        #: bounded wait for a task completion ack (seconds; configurable)
        self.task_ack_timeout: float = 30.0

    # ------------------------------------------------------------- registry
    @property
    def _acks(self) -> TaskAckRegistry:
        """The shared registry, lazily building a private one if unbound."""
        if self._ack_registry is None:
            self._ack_registry = TaskAckRegistry(self._bus)
        if self._ack_registry._scheduler is None and self._scheduler is not None:
            self._ack_registry._scheduler = self._scheduler  # timeout cancels
        return self._ack_registry

    def _client(self) -> Any:
        """The account's live client (None before start / after stop)."""
        return self._client_getter() if self._client_getter is not None else None

    @property
    def account_username(self) -> Optional[str]:
        """The account's own Discord username (None offline/pre-connect).

        Used to attribute Mudae's per-user limit messages ("**name**,
        the roulette is limited...") — a shared channel must not park OUR
        rolling on ANOTHER user's limit. Read defensively through the
        adapter's underlying library client.
        """
        client = self._client()
        user = getattr(client, "user", None)
        if user is None:
            user = getattr(getattr(client, "_client", None), "user", None)
        return getattr(user, "name", None)

    # ---------------------------------------------------------------- sends
    async def send_message(
        self, channel_id: int, content: str, *, priority: Priority = Priority.LOW
    ) -> None:
        """Send to a guild channel through the task pipeline (fire-and-ack).

        Returns ``None`` on success (see module docstring); raises
        :class:`SchedulerError` when the task fails, is cancelled, exceeds
        ``task_ack_timeout``, or when ``scheduler.submit`` rejects the task.
        """
        task = self._scheduler.submit(
            self._account, self._plugin, "message",
            channel_id=int(channel_id), content=content, priority=priority,
        )
        await self._acks.wait_for_task(task.id, self.task_ack_timeout)
        return None

    async def send_dm(
        self, user_id: int, content: str, *, priority: Priority = Priority.LOW
    ) -> None:
        """Send a DM through the task pipeline (fire-and-ack; see send_message)."""
        task = self._scheduler.submit(
            self._account, self._plugin, "dm",
            user_id=int(user_id), content=content, priority=priority,
        )
        await self._acks.wait_for_task(task.id, self.task_ack_timeout)
        return None

    async def open_dm(self, user_id: int) -> int:
        """Resolve (opening if needed) the DM channel id for a user.

        Cache-first across THREE layers: live memory -> the account's
        persistent api_cache row -> ONE client.open_dm (then recorded in
        both, so a DM is resolved once per account lifetime, not per boot).
        """
        return await resolve_dm_channel(
            self._cache, self._client(), int(user_id), self._account,
            self._api_cache,
        )

    async def add_reaction(self, channel_id: int, message_id: int, emoji: str,
                           *, priority: Priority = Priority.NORMAL) -> None:
        """React to a message through the paced pipeline (kakerareact).

        The emoji is the task payload; pacing/floor/lane/audit apply exactly
        like a send (safety first — reaction claim races are precisely where
        robotic timing gets flagged).
        """
        task = self._scheduler.submit(
            self._account, self._plugin, "reaction",
            channel_id=int(channel_id), message_id=int(message_id),
            content=str(emoji), priority=priority,
        )
        await self._acks.wait_for_task(task.id, self.task_ack_timeout)

    async def click_button(self, channel_id: int, message_id: int, custom_id: str,
                           *, priority: Priority = Priority.NORMAL) -> None:
        """Click a message component by custom_id (kakeraloot) — paced,
        audited, with the adapter resolving the live message on demand."""
        task = self._scheduler.submit(
            self._account, self._plugin, "button",
            channel_id=int(channel_id), message_id=int(message_id),
            content=str(custom_id), priority=priority,
        )
        await self._acks.wait_for_task(task.id, self.task_ack_timeout)

    async def fetch_user(self, user_id: int):
        """Resolve a user, cached per account in the database.

        Resolution order: the account's persistent api_cache 'user' row ->
        the client's fetch (then cached forever for this account). Returns a
        small namespace with ``id``/``name`` (or the raw client object when
        no repo is wired), None when the client cannot resolve the user.
        """
        user_id = int(user_id)
        if self._api_cache is not None:
            try:
                stored = await self._api_cache.get(self._account, "user", str(user_id))
            except Exception:
                stored = None
            if isinstance(stored, dict) and stored.get("id") == user_id:
                return SimpleNamespace(id=stored["id"], name=stored.get("name", ""))
        client = self._client()
        if client is None:
            return None
        user = await client.fetch_user(user_id)
        if user is not None and self._api_cache is not None:
            # UPSERT the fresh row (stale-start audit): the repository's
            # set() is INSERT .. ON CONFLICT DO UPDATE, so every
            # successful LIVE fetch refreshes the persisted row — a
            # renamed user heals on the next live fetch instead of
            # serving the stale name forever. (The read-through fast
            # path above is unchanged: a hit still costs zero API calls.)
            if isinstance(user, dict):
                fresh_name = str(user.get("name") or "")
            else:
                fresh_name = str(getattr(user, "name", "") or "")
            try:
                await self._api_cache.set(self._account, "user", str(user_id), {
                    "id": user_id,
                    "name": fresh_name,
                })
            except Exception:
                pass
        return user

    # ------------------------------------------------------------- cooldowns
    def _require_cooldowns(self) -> Any:
        """The shared CooldownService, or a clear error when unwired."""
        if self._cooldowns is None:
            raise PluginError(
                f"cooldown service is not wired for {self._account}/{self._plugin} "
                "(cooldowns require the framework's CooldownService)"
            )
        return self._cooldowns

    async def cooldown_set(self, key: str, duration_s: float, *,
                           source: str = "plugin", reason: str = "",
                           metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Start (or replace) a cooldown for THIS (account, plugin) pair.

        ``duration_s`` seconds from NOW; the absolute expiry is persisted,
        so the cooldown survives restarts with the correct remaining time.
        Returns the cooldown row (never contains token material).
        """
        return await self._require_cooldowns().set(
            self._account, self._plugin, key, duration_s,
            source=source, reason=reason, metadata=metadata,
        )

    async def cooldown_remaining(self, key: str) -> float:
        """Seconds left on a cooldown (0.0 when none/expired)."""
        return await self._require_cooldowns().remaining(
            self._account, self._plugin, key
        )

    async def cooldown_active(self, key: str) -> bool:
        """True when a cooldown is currently active for this key."""
        return await self._require_cooldowns().is_active(
            self._account, self._plugin, key
        )

    async def cooldown_clear(self, key: str) -> bool:
        """Remove one cooldown (memory + database); True when it existed."""
        return await self._require_cooldowns().clear(
            self._account, self._plugin, key
        )

    async def cooldown_list(self) -> List[Dict[str, Any]]:
        """Every ACTIVE cooldown of this (account, plugin) pair, soonest
        expiry first."""
        return await self._require_cooldowns().active_for(
            self._account, plugin=self._plugin
        )

    # ----------------------------------------------------------- plugin state
    def _require_plugin_state(self) -> Any:
        """The shared PluginStateService, or a clear error when unwired."""
        if self._plugin_state is None:
            raise PluginError(
                f"plugin-state service is not wired for "
                f"{self._account}/{self._plugin} (state persistence requires "
                "the framework's PluginStateService)"
            )
        return self._plugin_state

    async def state_get(self, key: str, default: Any = None) -> Any:
        """Read one persisted value from this (account, plugin) scope."""
        return await self._require_plugin_state().get_pair(
            self._account, self._plugin, key, default
        )

    async def state_set(self, key: str, value: Any) -> None:
        """Persist one value in this (account, plugin) scope (JSON value)."""
        await self._require_plugin_state().set_pair(
            self._account, self._plugin, key, value
        )

    async def state_delete(self, key: str) -> bool:
        """Delete one value from this (account, plugin) scope; True when it
        existed."""
        return await self._require_plugin_state().delete_pair(
            self._account, self._plugin, key
        )

    # -------------------------------------------------------------- inbound
    async def wait_for_message(
        self, check: Callable[[MessageReceived], bool], timeout: float
    ) -> Optional[MessageReceived]:
        """Wait for ONE matching inbound message; ``None`` on timeout.

        Fast-returns on the FIRST match (ported audit fix — it must not sit
        out the full window). ``check`` raising is surfaced as
        :class:`AccountError` with the cause instead of vanishing into the
        bus logger.
        """
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise AccountError(
                f"wait_for_message timeout must be > 0, got {timeout!r}"
            )
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[MessageReceived]" = loop.create_future()

        def _handler(event: Event) -> None:
            if not isinstance(event, MessageReceived) or event.account != self._account:
                return
            try:
                match = bool(check(event))
            except Exception as exc:  # surface raising checks to the waiter
                if not future.done():
                    future.set_exception(AccountError(
                        f"wait_for_message check failed on account "
                        f"{self._account!r} plugin {self._plugin!r}: "
                        f"{type(exc).__name__}: {exc}"
                    ))
                return
            if match and not future.done():
                future.set_result(event)

        # FINAL-PHASE F1-b′: through the shared per-account waiter index
        # when wired (one broadcast subscription at the manager; waiters
        # are invoked only for their own account's events), else the
        # legacy unfiltered bus subscription (direct-construction tests).
        if self._waiter_registry is not None:
            unsubscribe = self._waiter_registry.add(self._account, _handler)
        else:
            unsubscribe = self._bus.subscribe(MessageReceived, _handler)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            unsubscribe()

    async def collect_messages(
        self, check: Callable[[MessageReceived], bool], idle_timeout: float
    ) -> List[MessageReceived]:
        """Collect matching messages until ``idle_timeout`` passes unmatched.

        Idle-window semantics ported from the audited container: EVERY match
        resets the window, so a chatty sender keeps the collector alive while
        a quiet one ends it. A raising ``check`` raises :class:`AccountError`
        with the cause (ported audit fix — previously it silently looked like
        "no messages").
        """
        if not isinstance(idle_timeout, (int, float)) or idle_timeout <= 0:
            raise AccountError(
                f"collect_messages idle_timeout must be > 0, got {idle_timeout!r}"
            )
        queue: "asyncio.Queue[MessageReceived]" = asyncio.Queue()
        failure: List[BaseException] = []

        def _handler(event: Event) -> None:
            if not isinstance(event, MessageReceived) or event.account != self._account:
                return
            try:
                match = bool(check(event))
            except Exception as exc:
                if not failure:
                    failure.append(exc)
                    # stale-start audit: wake the collector NOW — it is
                    # parked inside queue.get() and would otherwise sit
                    # out the full idle window despite the failure. The
                    # sentinel is discarded by the post-get failure check.
                    queue.put_nowait(None)
                return
            if match:
                queue.put_nowait(event)

        if self._waiter_registry is not None:
            unsubscribe = self._waiter_registry.add(self._account, _handler)
        else:
            unsubscribe = self._bus.subscribe(MessageReceived, _handler)
        collected: List[MessageReceived] = []
        try:
            while True:
                if failure:
                    # stale-start audit: checked BEFORE parking on
                    # queue.get — a raising check must surface
                    # immediately, not after the idle window.
                    break
                try:  # each match restarts the idle window
                    event = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    break
                if failure:
                    break  # woken by the failure sentinel, not a match
                collected.append(event)
            if failure:
                exc = failure[0]
                raise AccountError(
                    f"collect_messages check failed on account "
                    f"{self._account!r} plugin {self._plugin!r}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            return collected
        finally:
            unsubscribe()

    # -------------------------------------------------------------- passthru
    def publish(self, event: Event) -> None:
        """Fire-and-forget publish on the shared bus (safe from sync code in a loop)."""
        self._bus.publish_nowait(event)

    def validate_channel(self, channel_id: int) -> None:
        """Raise :class:`core.errors.TargetGone` when the channel is unusable."""
        self._cache.validate_channel(int(channel_id))

    def describe_channel(self, channel_id: int) -> str:
        """Human-readable channel label (never raises)."""
        return self._cache.describe(int(channel_id))

    # ------------------------------------------------------------ properties
    @property
    def credentials(self) -> Any:
        """Fleet credentials (mudae id, owner ids) — never hardcoded."""
        return self._credentials

    @property
    def account(self) -> str:
        """Account name this facade belongs to."""
        return self._account

    @property
    def plugin(self) -> str:
        """Plugin name this facade was created for."""
        return self._plugin

    @property
    def settings(self) -> Dict[str, Any]:
        """LIVE resolved settings dict — refreshed in place by the plugin manager."""
        return self._settings_dict

    def __repr__(self) -> str:  # debug rendering
        return f"<AccountPluginServices {self._account}:{self._plugin}>"


__all__ = ["AccountPluginServices", "TaskAckRegistry"]
