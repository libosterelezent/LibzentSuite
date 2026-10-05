"""Event bus + the framework's typed event vocabulary (spec §11).

Guarantees (ported from the audited original):
  * one failing handler never breaks other handlers (isolation),
  * subscribers registered for a base class receive subclass events,
  * publish() can be awaited, publish_nowait() is safe from sync callbacks,
  * the shared dispatcher task is strongly referenced (no GC mid-dispatch).
"""
from __future__ import annotations

import asyncio
import collections
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from ._asyncutil import outer_cancel_pending

LOG = logging.getLogger("slaughtermudae.events")

Handler = Callable[["Event"], Any]


@dataclass
class Event:
    """Base event. ts defaults to wall clock; account is "" when not per-account."""

    account: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def name(self) -> str:
        return type(self).__name__


# ---------------------------------------------------------------- accounts
@dataclass
class AccountConnected(Event):
    """An account's client connected and its cache was ingested."""


@dataclass
class AccountDisconnected(Event):
    """An account's client dropped (reason carries the why)."""

    reason: str = ""


@dataclass
class AccountFailed(Event):
    """An account failed to start or reconnect."""

    error: str = ""


# ---------------------------------------------------------------- plugins
@dataclass
class PluginLoaded(Event):
    """A plugin instance was created + loaded on an account."""

    plugin: str = ""


@dataclass
class PluginUnloaded(Event):
    """A plugin instance was unloaded from an account."""

    plugin: str = ""


@dataclass
class PluginEnabledChanged(Event):
    """A plugin was enabled or disabled on an account."""

    plugin: str = ""
    enabled: bool = True


# ------------------------------------------------------------------ tasks
@dataclass
class TaskQueued(Event):
    """A task entered the scheduler queue."""

    task_id: int = 0
    plugin: str = ""
    action: str = ""


@dataclass
class TaskStarted(Event):
    """A worker picked the task up (rate grant acquired, sender starting)."""

    task_id: int = 0
    action: str = ""


@dataclass
class TaskCompleted(Event):
    """A task finished successfully."""

    task_id: int = 0
    duration_s: float = 0.0
    summary: str = ""


@dataclass
class TaskFailed(Event):
    """A task failed permanently (retries exhausted or non-retryable).

    ``error_kind`` is the exception CLASS NAME (e.g. "TargetGone") so the
    services layer can faithfully re-raise FlowBlocked subclasses to the
    waiting plugin — the flow-abort contract must survive the task-ack seam.
    """

    task_id: int = 0
    error: str = ""
    error_kind: str = ""


@dataclass
class TaskCancelled(Event):
    """A queued/running task was cancelled."""

    task_id: int = 0


# ------------------------------------------------------------- rate limits
@dataclass
class RateLimitTriggered(Event):
    """Pacing or a server-provided limit delayed an action."""

    action: str = ""
    wait_s: float = 0.0
    reason: str = ""  # "pacing" | "server" | "lane"


# ---------------------------------------------------------------- settings
@dataclass
class SettingChanged(Event):
    """One setting changed somewhere (plugin default layer, account layer...)."""

    scope: str = ""       # e.g. "account:alpha/plugin:soulcopy/key:trigger"
    key: str = ""
    old_value: Any = None
    new_value: Any = None


# ---------------------------------------------------------------- messages
@dataclass
class MessageReceived(Event):
    """A Discord message seen by an account (never the account's own sends).

    COMPATIBILITY: the five flat fields are the original surface every
    existing consumer uses (soulcopy, roll, remote control, collect/wait).
    ``message`` carries the RICH adapters.model.DiscordMessage (embeds,
    components, attachments, reactions, reference, author/channel/guild,
    created_at, and the runtime-only ``raw`` escape hatch) — typed ``Any``
    so core never imports the adapter layer. None for legacy/synthetic
    events that predate the rich boundary.
    """

    message_id: int = 0
    channel_id: int = 0
    author_id: int = 0
    content: str = ""
    dm: bool = False
    #: rich DTO (adapters.model.DiscordMessage) — additive, optional
    message: Any = None


@dataclass
class MessageEdited(Event):
    """A message was edited (stable ids + the RICH post-edit message)."""

    message_id: int = 0
    channel_id: int = 0
    guild_id: Optional[int] = None
    message: Any = None  # rich post-edit DiscordMessage (raw = library msg)


@dataclass
class MessageDeleted(Event):
    """A message was deleted (ids only — the library gives no payload body)."""

    message_id: int = 0
    channel_id: int = 0
    guild_id: Optional[int] = None


@dataclass
class ReactionAdded(Event):
    """A reaction was added to a message — WITHOUT any message fetch (§9).

    Everything a kakerareact-style plugin needs arrives on the event:
    message/channel/guild ids, the reacting user, the printable emoji, dm
    flag, and the optional runtime-only ``raw`` library objects.
    """

    message_id: int = 0
    channel_id: int = 0
    guild_id: Optional[int] = None
    user_id: int = 0
    emoji: str = ""
    dm: bool = False
    raw: Any = field(default=None, repr=False)


# -------------------------------------------------------------- simulation
@dataclass
class ScenarioStarted(Event):
    """A simulation scenario began."""

    name: str = ""


@dataclass
class ScenarioFinished(Event):
    """A simulation scenario ended (passed flag included)."""

    name: str = ""
    passed: bool = False
    duration_s: float = 0.0


# ------------------------------------------------------------ event planes
#: Control/lifecycle plane — the typed vocabulary that must survive a
#: message-flood overflow (task lifecycle, account lifecycle, plugin
#: lifecycle, rate limits, settings, simulation bookends). These get a
#: small reserved admission lane above ``EventBus.MAX_QUEUE`` so a burst
#: of MessageReceived can never starve shutdown/teardown tail events
#: (AccountDisconnected / TaskFailed / ...). Message-plane types and any
#: unclassified ``Event`` subclass keep the flood-plane drop-newest policy.
_CONTROL_TYPES = frozenset({
    AccountConnected, AccountDisconnected, AccountFailed,
    PluginLoaded, PluginUnloaded, PluginEnabledChanged,
    TaskQueued, TaskStarted, TaskCompleted, TaskFailed, TaskCancelled,
    RateLimitTriggered, SettingChanged,
    ScenarioStarted, ScenarioFinished,
})
#: subclass-aware fallback (the exact-type frozenset hit covers the hot path)
_CONTROL_ISINSTANCE = tuple(_CONTROL_TYPES)


def _is_control(event: "Event") -> bool:
    """Plane test: exact-type fast path, isinstance for typed subclasses."""
    return (type(event) in _CONTROL_TYPES
            or isinstance(event, _CONTROL_ISINSTANCE))


class _HandlerList(list):
    """Handler list with a mutation version + cached dispatch snapshot.

    FINAL-PHASE (G6-A′): publish() used to copy ``list(handlers)`` per
    matching type per event; the snapshot is now rebuilt ONLY when the
    list mutated since the last copy (version counter — the F2 idiom).
    Semantics identical to the unconditional copy: the snapshot is taken
    at reach time, so a handler unsubscribed by an EARLIER type's handler
    in the same publish call is still skipped (verified by test).
    """

    __slots__ = ("ver", "snap", "snapver")

    def __init__(self, iterable=()):
        super().__init__(iterable)
        self.ver = 0
        self.snap: List[Handler] = []
        self.snapver = -1

    def append(self, handler) -> None:
        self.ver += 1
        super().append(handler)

    def remove(self, handler) -> None:  # raises ValueError like list
        self.ver += 1
        super().remove(handler)

    def snapshot(self) -> List[Handler]:
        if self.snapver != self.ver:
            self.snap = list(self)
            self.snapver = self.ver
        return self.snap


class EventBus:
    """Minimal async pub/sub with subclass-aware dispatch and error isolation.

    FINAL-PHASE (F5-c): ``publish_nowait`` no longer creates one Task per
    event (measured ~49µs/event of pure task machinery on the reference
    box — 65% of the lean dispatch path, and the source of the ~22ms
    burst-callback lag ceiling). Events append to a bounded FIFO drained
    by ONE pending dispatcher task that yields every ``_slice_events``
    events (or ``_slice_budget_s``), so a burst occupies the loop in
    bounded slices instead of one long callback. Overflow is two-tier
    (round-4/F4): flood-plane events (message traffic, unclassified)
    drop the NEWEST event at ``MAX_QUEUE`` — delivered sequences stay
    FIFO, counted and logged burst-limited — while control-plane types
    (``_CONTROL_TYPES``: task/account/plugin lifecycle, rate limits,
    settings, simulation bookends) are admitted up to
    ``MAX_QUEUE + CONTROL_RESERVE``, so a message flood cannot starve
    shutdown tail events.
    """

    #: queue bound — headroom above the 10k PersistQueue cap (messages are
    #: the flood class); at ~150µs/event fully-loaded this is ~1.5s of
    #: sustained maximal burst before drops begin.
    MAX_QUEUE = 20_000
    #: reserved admission headroom for control-plane events above the
    #: flood cap — flood classes cannot occupy this lane, so at least
    #: CONTROL_RESERVE control events always find room under overflow.
    CONTROL_RESERVE = 1_024
    #: wall-seconds an async handler may hold the shared dispatcher before
    #: a slow-subscriber warning is logged (documented head-of-line
    #: contract — logged only, never cancelled).
    SLOW_HANDLER_S = 1.0
    #: dispatch slice: events per loop turn before yielding
    SLICE_EVENTS = 128
    #: dispatch slice: wall-seconds per loop turn before yielding
    SLICE_BUDGET_S = 0.004

    def __init__(self) -> None:
        self._subs: Dict[Type[Event], List[Handler]] = {}
        # FINAL-PHASE (G6-A′): dispatch-list cache per concrete event type
        # (invalidated wholesale on any subscribe/unsubscribe — rare,
        # boot/attach-time events). Removes the per-event items-snapshot +
        # isinstance chain (measured 1.19µs/event at the production
        # 10-type shape). Ordering identical: dict insertion order of
        # matching types, list order within a type.
        self._type_cache: Dict[Type[Event], tuple] = {}
        self._queue: "collections.deque[Event]" = collections.deque()
        self._dispatcher: Optional["asyncio.Task[None]"] = None
        self._dropped = 0
        self._dispatched = 0
        self._drop_log_gate = 0.0

    def subscribe(self, event_type: Type[Event], handler: Handler) -> Callable[[], None]:
        """Register handler for event_type (and its subclasses).

        Returns an unsubscribe callable (idempotent).

        SYNC-HANDLER CONTRACT (round-4/F4): dispatch is ordered and
        inline — handlers run sequentially per event. One slow SYNC
        handler blocks every later handler for that event, and one slow
        ASYNC handler parks the shared dispatcher (head-of-line for the
        whole bus) until it returns. Async handlers slower than
        ``SLOW_HANDLER_S`` are logged (never cancelled); offload real
        I/O-heavy work to your own task instead of awaiting it here.
        """
        cell = self._subs.get(event_type)
        if cell is None:
            cell = self._subs[event_type] = _HandlerList()
        cell.append(handler)
        self._type_cache.clear()

        def _unsubscribe() -> None:
            live = self._subs.get(event_type)
            if live is not None:
                try:
                    live.remove(handler)
                except ValueError:
                    pass
                self._type_cache.clear()

        return _unsubscribe

    async def drain(self, timeout: float = 2.0) -> None:
        """Await the fire-and-forget dispatch backlog (shutdown tail).

        publish_nowait defers dispatch to the coalesced dispatcher; at
        shutdown those orphans would be cancelled by loop close — this
        makes tail-event delivery (AccountDisconnected etc.) contractual,
        not lucky. Re-checks the queue until stable (a handler publishing
        during the drain is flushed by the same call).

        Round-4 (F4/1): a dispatcher that DIED with a backlog (outer
        cancel requeue path, or a handler that raised GeneratorExit)
        used to turn the no-pending branch into ``await sleep(0)`` busy
        spin (measured 130k turns/s) until the deadline, undelivered.
        The branch now retrieves + logs the dead task's exception and
        resurrects a fresh dispatcher to deliver the backlog.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            dispatcher = self._dispatcher
            if dispatcher is not None and dispatcher.done():
                if not self._queue:
                    return  # clean exit: dispatcher idle, nothing queued
                self._retire_dispatcher(dispatcher, "drain-resurrect")
                dispatcher = self._dispatcher = loop.create_task(
                    self._drain_queue(), name="eventbus-dispatch")
            elif dispatcher is None:
                if not self._queue:
                    return
                dispatcher = self._dispatcher = loop.create_task(
                    self._drain_queue(), name="eventbus-dispatch")
            await asyncio.wait(
                [dispatcher], timeout=max(0.0, deadline - loop.time()))
            if loop.time() >= deadline:
                if self._queue:
                    LOG.warning(
                        "bus drain timeout: %d events undelivered, "
                        "%d dropped-total", len(self._queue), self._dropped)
                return

    def _retire_dispatcher(self, task: Optional["asyncio.Task[None]"],
                           context: str) -> None:
        """Retrieve + log a dead dispatcher's exception before replacing it.

        Round-4 (F4/3): without this, a dispatcher that died on an
        abnormal exit (GeneratorExit from a handler, an outright bug)
        left its exception unretrieved — the silent-death mode: the bus
        looks idle while backlog sits behind a corpse.
        """
        if task is None or not task.done() or task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            LOG.error("event dispatcher died before %s: %r",
                      context, exc, exc_info=exc)

    def publish_nowait(self, event: Event) -> Optional["asyncio.Task[None]"]:
        """Fire-and-forget publish; safe from sync callbacks inside a loop.

        Returns the shared dispatcher task (or None on drop). No caller
        consumes the return value (verified); the annotation change from
        Task[List[Any]] is interface-level only.

        Overflow is two-tier (round-4/F4/2): flood-plane events (message
        traffic and unclassified Events) drop-newest at ``MAX_QUEUE``
        exactly as before; control-plane events (``_CONTROL_TYPES`` —
        task/account/plugin lifecycle, rate limits, settings, simulation
        bookends) are admitted up to ``MAX_QUEUE + CONTROL_RESERVE``, so
        a message flood cannot starve shutdown tail events.
        """
        limit = self.MAX_QUEUE
        plane = "control" if _is_control(event) else "flood"
        if plane == "control":
            limit += self.CONTROL_RESERVE
        if len(self._queue) >= limit:
            self._dropped += 1
            now = time.monotonic()
            if now - self._drop_log_gate > 1.0:  # burst-limited logging
                self._drop_log_gate = now
                LOG.error(
                    "event bus overflow: dropped %s (plane=%s, queued=%d, "
                    "dropped_total=%d)", type(event).__name__, plane,
                    len(self._queue), self._dropped)
            return self._dispatcher
        self._queue.append(event)
        dispatcher = self._dispatcher
        if dispatcher is None or dispatcher.done():
            self._retire_dispatcher(dispatcher, "publish_nowait")
            self._dispatcher = asyncio.get_running_loop().create_task(
                self._drain_queue(), name="eventbus-dispatch")
        return self._dispatcher

    async def _drain_queue(self) -> None:
        """Drain the FIFO in bounded slices (the burst-lag fix)."""
        slice_start = time.monotonic()
        count_in_slice = 0
        while self._queue:
            # empty-check and exit happen in the SAME step (no await between)
            # so a publish racing the exit cannot lose its wakeup
            event = self._queue.popleft()
            self._dispatched += 1
            try:
                await self.publish(event)
            except asyncio.CancelledError:
                if outer_cancel_pending(unknown=True):
                    self._queue.appendleft(event)  # put it back; we're dying
                    raise
                continue  # internal handler cancel: already isolated+logged
            count_in_slice += 1
            if (count_in_slice >= self.SLICE_EVENTS
                    or time.monotonic() - slice_start >= self.SLICE_BUDGET_S):
                count_in_slice = 0
                slice_start = time.monotonic()
                await asyncio.sleep(0)  # yield the loop: bounded occupancy

    def stats(self) -> Dict[str, int]:
        """Queue depth + counters (dashboard/observability)."""
        return {"queued": len(self._queue), "dispatched": self._dispatched,
                "dropped": self._dropped}

    async def publish(self, event: Event) -> List[Any]:
        """Invoke all matching handlers (sync or async), isolated per handler.

        Returns handler results / exception objects in invocation order.
        An in-handler CancelledError counts as a handler failure unless the
        publish task itself is being cancelled; KeyboardInterrupt/SystemExit
        always propagate (process-level signals).

        CAVEAT (round-4/F4/5): this DIRECT await bypasses the bounded
        FIFO — it dispatches inline in the caller's step and returns
        handler results. All production callers publish via
        publish_nowait; direct publish() is for tests and for code that
        genuinely needs the results list.
        """
        results: List[Any] = []
        key = type(event)
        cells = self._type_cache.get(key)
        if cells is None:  # one-time per (type, subscription-epoch): O(S)
            cells = tuple(h for t, h in self._subs.items()
                          if issubclass(key, t))
            self._type_cache[key] = cells
        for cell in cells:
            for handler in cell.snapshot():
                try:
                    outcome = handler(event)
                    if inspect.iscoroutine(outcome):
                        # head-of-line observability (F4/4): the shared
                        # dispatcher is parked for the whole await — log
                        # (never cancel) when a subscriber overruns
                        started = time.monotonic()
                        outcome = await outcome
                        elapsed = time.monotonic() - started
                        if elapsed > self.SLOW_HANDLER_S:
                            LOG.warning(
                                "slow async event handler %r held the bus "
                                "%.3fs for %s (>%.0fs; head-of-line — see "
                                "subscribe() contract)", handler, elapsed,
                                type(event).__name__, self.SLOW_HANDLER_S)
                    results.append(outcome)
                except asyncio.CancelledError:
                    # read-only check (the old cancelling()>uncancel() idiom
                    # consumed the very cancellation it was inspecting);
                    # on 3.10 (no Task.cancelling) re-raising is the safe side
                    if outer_cancel_pending(unknown=True):
                        raise
                    LOG.exception(
                        "event handler %r cancelled internally for %s", handler, type(event).__name__
                    )
                    results.append(asyncio.CancelledError("handler cancelled"))
                except (KeyboardInterrupt, SystemExit, GeneratorExit):
                    # process-level signals / a close() aimed at THIS
                    # coroutine must not be swallowed as handler failures
                    raise
                except BaseException as exc:  # isolation by design
                    LOG.exception("event handler %r failed for %s", handler, type(event).__name__)
                    results.append(exc)
        return results

__all__ = [
    "EventBus", "Event", "AccountConnected", "AccountDisconnected", "AccountFailed",
    "PluginLoaded", "PluginUnloaded", "PluginEnabledChanged", "TaskQueued",
    "TaskStarted", "TaskCompleted", "TaskFailed", "TaskCancelled",
    "RateLimitTriggered", "SettingChanged", "MessageReceived",
    "MessageEdited", "MessageDeleted", "ReactionAdded",
    "ScenarioStarted", "ScenarioFinished",
]
