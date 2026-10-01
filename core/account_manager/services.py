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
        carrying the reason, a timeout raises ``SchedulerError`` too,

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

LOG = logging.getLogger("slaughtermudae.services")


class TaskAckRegistry:
    """task_id -> future map fed by the bus Task* events (one subscription)."""

    def __init__(self, bus: EventBus) -> None:
        """Subscribe the three completion events on ``bus`` (idempotent per bus)."""
        self._bus = bus
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
    #: plugin-side catch (soulcopy's flow-abort contract) actually fires
    _FLOW_CLASSES = {
        "FlowBlocked": FlowBlocked,
        "CacheInvalid": CacheInvalid,
        "TargetGone": TargetGone,
        "AccessDenied": AccessDenied,
        "FetchRequired": FetchRequired,
    }

    def _on_failed(self, event: TaskFailed) -> None:
        future = self._futures.pop(event.task_id, None)
        if future is None or future.done():
            return
        blocked = self._FLOW_CLASSES.get(getattr(event, "error_kind", "") or "")
        if blocked is not None:
            future.set_exception(
                blocked(event.account, self._flow_reason(event.error))
            )
        else:
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
        """Create + register the future for ``task_id`` (loop-safe)."""
        future: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
        self._futures[task_id] = future
        return future

    async def wait_for_task(self, task_id: int, timeout: float) -> Any:
        """Await one task's completion event; returns the TaskCompleted summary.

        Raises:
            SchedulerError: the task failed, was cancelled, or did not
                produce a completion event within ``timeout`` seconds.
        """
        future = self.register(task_id)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._futures.pop(task_id, None)
            raise SchedulerError(
                f"task {task_id} did not complete within {timeout:.0f}s"
            ) from None

    def pending(self) -> List[int]:
        """Task ids currently awaited (introspection/tests)."""
        return sorted(self._futures)


class AccountPluginServices:
    """Per-(account, plugin) facade implementing the PluginServices protocol.

    Built by :meth:`core.account_manager.AccountManager.services_for`. Two
    optional keyword arguments extend the pinned constructor (defaults keep
    the pinned signature fully usable):

      * ``ack_registry`` — shared TaskAckRegistry (the manager passes its
        own so the bus is subscribed exactly once),
      * ``client_getter`` — zero-arg callable returning the account's LIVE
        client (or None); needed for cache-miss DM opens.
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
        client_getter: Optional[Callable[[], Any]] = None,
        api_cache_repo: Any = None,
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
        self._client_getter = client_getter
        #: per-account persistent entity cache (database.cache repository)
        self._api_cache = api_cache_repo
        #: bounded wait for a task completion ack (seconds; configurable)
        self.task_ack_timeout: float = 30.0

    # ------------------------------------------------------------- registry
    @property
    def _acks(self) -> TaskAckRegistry:
        """The shared registry, lazily building a private one if unbound."""
        if self._ack_registry is None:
            self._ack_registry = TaskAckRegistry(self._bus)
        return self._ack_registry

    def _client(self) -> Any:
        """The account's live client (None before start / after stop)."""
        return self._client_getter() if self._client_getter is not None else None

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
                stored = self._api_cache.get(self._account, "user", str(user_id))
            except Exception:
                stored = None
            if isinstance(stored, dict) and stored.get("id") == user_id:
                return SimpleNamespace(id=stored["id"], name=stored.get("name", ""))
        client = self._client()
        if client is None:
            return None
        user = await client.fetch_user(user_id)
        if user is not None and self._api_cache is not None:
            try:
                self._api_cache.set(self._account, "user", str(user_id), {
                    "id": user_id,
                    "name": getattr(user, "name", "") or "",
                })
            except Exception:
                pass
        return user

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
                return
            if match:
                queue.put_nowait(event)

        unsubscribe = self._bus.subscribe(MessageReceived, _handler)
        collected: List[MessageReceived] = []
        try:
            while True:
                try:  # each match restarts the idle window
                    event = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    break
                if failure:
                    break
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
