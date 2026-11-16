"""Event bus + the framework's typed event vocabulary (spec §11).

Guarantees (ported from the audited original):
  * one failing handler never breaks other handlers (isolation),
  * subscribers registered for a base class receive subclass events,
  * publish() can be awaited, publish_nowait() is safe from sync callbacks,
  * fire-and-forget tasks hold strong references (no GC mid-dispatch).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ._asyncutil import cancellation_attributable, outer_cancel_pending

LOG = logging.getLogger("libzent.events")

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

    scope: str = ""  # e.g. "account:alpha/plugin:probe/key:trigger"
    key: str = ""
    old_value: Any = None
    new_value: Any = None


# ---------------------------------------------------------------- messages
@dataclass
class MessageReceived(Event):
    """A Discord message seen by an account (never the account's own sends).

    COMPATIBILITY: the five flat fields are the original surface every
    existing consumer uses (flow plugins, remote control, collect/wait).
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
    guild_id: int | None = None
    message: Any = None  # rich post-edit DiscordMessage (raw = library msg)


@dataclass
class MessageDeleted(Event):
    """A message was deleted (ids only — the library gives no payload body)."""

    message_id: int = 0
    channel_id: int = 0
    guild_id: int | None = None


@dataclass
class ReactionAdded(Event):
    """A reaction was added to a message — WITHOUT any message fetch (§9).

    Everything a kakerareact-style plugin needs arrives on the event:
    message/channel/guild ids, the reacting user, the printable emoji, dm
    flag, and the optional runtime-only ``raw`` library objects.
    """

    message_id: int = 0
    channel_id: int = 0
    guild_id: int | None = None
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


@dataclass
class FleetStateChanged(Event):
    """A fleet orchestration transition landed (PHASE 9, spec §3).

    Published by FleetService's publisher seam AFTER the transition is
    persisted (write-ahead); WS clients receive it as a ``fleet`` frame
    and the Fleet Hub converges. Delivery is an optimization — the
    fleet_transitions table is the durable record.
    """

    kind: str = ""
    group: str = ""
    reason: str = ""
    generation: int = 0


class _HandlerList(list):
    """Handler list with a mutation version + cached dispatch snapshot.

    FINAL-PHASE (G6-A′): publish() used to copy ``list(handlers)`` per
    matching type per event; the snapshot is now rebuilt ONLY when the
    list mutated since the last copy (version counter — the F2 idiom).
    Semantics identical to the unconditional copy: the snapshot is taken
    at reach time, so a handler unsubscribed by an EARLIER type's handler
    in the same publish call is still skipped (verified by test).
    """

    __slots__ = ("snap", "snapver", "ver")

    def __init__(self, iterable=()):
        super().__init__(iterable)
        self.ver = 0
        self.snap: list[Handler] = []
        self.snapver = -1

    def append(self, handler) -> None:
        self.ver += 1
        super().append(handler)

    def remove(self, handler) -> None:  # raises ValueError like list
        self.ver += 1
        super().remove(handler)

    def snapshot(self) -> list[Handler]:
        if self.snapver != self.ver:
            self.snap = list(self)
            self.snapver = self.ver
        return self.snap


#: CONTROL-PLANE event names: terminal Task* acks + account lifecycle.
#: These ride a SEPARATE small queue that is never subject to the main
#: FIFO's drop-newest overflow: a dropped TaskCompleted/TaskFailed/
#: TaskCancelled permanently breaks the plugin ack contract (the waiter
#: hangs for its 30s timeout, reports a false failure, and the plugin's
#: own retry DUPLICATES the send). Account* state events drive supervisor
#: reconnect decisions — same class of harm. High-volume telemetry
#: (TaskQueued/TaskStarted) stays on the main queue deliberately.
CONTROL_PLANE_EVENTS: frozenset[str] = frozenset(
    {
        "TaskCompleted",
        "TaskFailed",
        "TaskCancelled",
        "AccountConnected",
        "AccountDisconnected",
        "AccountFailed",
    }
)


class EventBus:
    """Minimal async pub/sub with subclass-aware dispatch and error isolation.

    FINAL-PHASE (F5-c): ``publish_nowait`` no longer creates one Task per
    event (measured ~49µs/event of pure task machinery on the reference
    box — 65% of the lean dispatch path, and the source of the ~22ms
    burst-callback lag ceiling). Events append to a bounded FIFO drained
    by ONE pending dispatcher task that yields every ``_slice_events``
    events (or ``_slice_budget_s``), so a burst occupies the loop in
    bounded slices instead of one long callback. Overflow drops the
    NEWEST event (delivered sequences stay FIFO), counts it, and logs
    burst-limited — today's unbounded background growth under a message
    flood becomes a bounded, observable degradation.

    CONTROL-PLANE queue: terminal Task*/Account* events (see
    CONTROL_PLANE_EVENTS) bypass that FIFO into a dedicated bounded deque
    drained FIRST by the same dispatcher — a message flood can degrade
    telemetry but must never drop an ack.
    """

    #: queue bound — headroom above the 10k PersistQueue cap (messages are
    #: the flood class); at ~150µs/event fully-loaded this is ~1.5s of
    #: sustained maximal burst before drops begin.
    MAX_QUEUE = 20_000
    #: control-plane queue bound — sized orders of magnitude above the
    #: terminal-event rate (subscribers are sync; the queue only holds
    #: events between dispatcher slices). Overflow logs CRITICALLY: it
    #: means the dispatcher is wedged, not that the load is high.
    CONTROL_MAX_QUEUE = 1_024
    #: dispatch slice: events per loop turn before yielding
    SLICE_EVENTS = 128
    #: dispatch slice: wall-seconds per loop turn before yielding
    SLICE_BUDGET_S = 0.004

    def __init__(self, *, is_control_plane: Callable[[Event], bool] | None = None) -> None:
        self._subs: dict[type[Event], list[Handler]] = {}
        # FINAL-PHASE (G6-A′): dispatch-list cache per concrete event type
        # (invalidated wholesale on any subscribe/unsubscribe — rare,
        # boot/attach-time events). Removes the per-event items-snapshot +
        # isinstance chain (measured 1.19µs/event at the production
        # 10-type shape). Ordering identical: dict insertion order of
        # matching types, list order within a type.
        self._type_cache: dict[type[Event], tuple] = {}
        self._queue: collections.deque[Event] = collections.deque()
        self._control_queue: collections.deque[Event] = collections.deque()
        self._is_control: Callable[[Event], bool] = (
            is_control_plane
            if is_control_plane is not None
            else lambda event: type(event).__name__ in CONTROL_PLANE_EVENTS
        )
        self._dispatcher: asyncio.Task[None] | None = None
        self._dropped = 0
        self._control_dropped = 0
        self._unattributed_cancels = 0
        self._dispatched = 0
        self._drop_log_gate = 0.0

    def subscribe(self, event_type: type[Event], handler: Handler) -> Callable[[], None]:
        """Register handler for event_type (and its subclasses).

        Returns an unsubscribe callable (idempotent).
        """
        cell = self._subs.get(event_type)
        if cell is None:
            cell = self._subs[event_type] = _HandlerList()
        cell.append(handler)
        self._type_cache.clear()

        def _unsubscribe() -> None:
            live = self._subs.get(event_type)
            if live is not None:
                with contextlib.suppress(ValueError):
                    live.remove(handler)
                self._type_cache.clear()

        return _unsubscribe

    async def drain(self, timeout: float = 2.0) -> None:
        """Await the fire-and-forget dispatch backlog (shutdown tail).

        publish_nowait defers dispatch to the coalesced dispatcher; at
        shutdown those orphans would be cancelled by loop close — this
        makes tail-event delivery (AccountDisconnected etc.) contractual,
        not lucky. Re-checks both queues until stable (a handler
        publishing during the drain is flushed by the same call).

        A dispatcher that DIED with events still queued (e.g. a 3.10
        unattributable handler cancellation before that path was made
        isolating) is respawned here instead of hot-spinning: the old
        ``await asyncio.sleep(0)`` no-pending branch burned the CPU
        without making progress.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            dispatcher = self._dispatcher
            if (dispatcher is None or dispatcher.done()) and (self._queue or self._control_queue):
                # dead dispatcher + backlog: respawn it (bounded by the
                # caller's timeout — never a hot spin)
                dispatcher = self._dispatcher = loop.create_task(
                    self._drain_queue(), name="eventbus-dispatch"
                )
            if dispatcher is not None and not dispatcher.done():
                await asyncio.wait({dispatcher}, timeout=max(0.0, deadline - loop.time()))
            elif not self._queue and not self._control_queue:
                return
            else:  # pragma: no cover - defensive: never hot-spin
                await asyncio.sleep(0.005)
            if loop.time() >= deadline:
                if self._queue or self._control_queue:
                    LOG.warning(
                        "bus drain timeout: %d events undelivered "
                        "(+%d undelivered control-plane, %d dropped-total)",
                        len(self._queue),
                        len(self._control_queue),
                        self._dropped,
                    )
                return

    def publish_nowait(self, event: Event) -> asyncio.Task[None] | None:
        """Fire-and-forget publish; safe from sync callbacks inside a loop.

        Control-plane events (CONTROL_PLANE_EVENTS) route to the dedicated
        never-starved deque; everything else uses the drop-newest FIFO.

        Returns the shared dispatcher task (or None on drop). No caller
        consumes the return value (verified); the annotation change from
        Task[List[Any]] is interface-level only.
        """
        if self._is_control(event):
            if len(self._control_queue) >= self.CONTROL_MAX_QUEUE:
                # Must never happen (subscribers are sync) — if it does,
                # the dispatcher is wedged: say so as loudly as we can.
                self._control_dropped += 1
                LOG.critical(
                    "CONTROL-PLANE event bus overflow: dropped %s "
                    "(control_queued=%d, control_dropped_total=%d) — "
                    "the dispatcher is wedged, not merely busy",
                    type(event).__name__,
                    len(self._control_queue),
                    self._control_dropped,
                )
                return self._dispatcher
            self._control_queue.append(event)
        else:
            if len(self._queue) >= self.MAX_QUEUE:
                self._dropped += 1
                now = time.monotonic()
                if now - self._drop_log_gate > 1.0:  # burst-limited logging
                    self._drop_log_gate = now
                    LOG.error(
                        "event bus overflow: dropped %s (queued=%d, dropped_total=%d)",
                        type(event).__name__,
                        len(self._queue),
                        self._dropped,
                    )
                return self._dispatcher
            self._queue.append(event)
        if self._dispatcher is None or self._dispatcher.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # no loop: the caller treats this as "not published" (and
                # logs it dropped) — un-enqueue, or the event would still
                # be delivered later by the next publish's dispatcher
                target = self._control_queue if self._is_control(event) else self._queue
                if target and target[-1] is event:
                    target.pop()
                raise
            self._dispatcher = loop.create_task(self._drain_queue(), name="eventbus-dispatch")
        return self._dispatcher

    async def _drain_queue(self) -> None:
        """Drain both FIFOs in bounded slices (the burst-lag fix).

        The control-plane deque is drained FIRST at every pop: acks
        out-rank telemetry under load.
        """
        slice_start = time.monotonic()
        count_in_slice = 0
        while self._control_queue or self._queue:
            # empty-check and exit happen in the SAME step (no await between)
            # so a publish racing the exit cannot lose its wakeup
            event = self._control_queue.popleft() if self._control_queue else self._queue.popleft()
            self._dispatched += 1
            try:
                await self.publish(event)
            except asyncio.CancelledError:
                if cancellation_attributable() and outer_cancel_pending(unknown=True):
                    # requeue onto the lane the event came from — a
                    # control-plane ack re-queued onto the main deque
                    # lost its drop protection
                    if self._is_control(event):
                        self._control_queue.appendleft(event)
                    else:
                        self._queue.appendleft(event)
                    raise
                # 3.10 (no Task.cancelling): the cancel is unattributable.
                # Requeue-and-die here used to WEDGE the bus — every
                # respawned dispatcher popped the same event and died
                # again. Drop with accounting instead and keep draining.
                self._unattributed_cancels += 1
                LOG.error(
                    "dispatcher survived an unattributable cancellation; "
                    "dropped %s (accounted, total=%d)",
                    type(event).__name__,
                    self._unattributed_cancels,
                )
                continue
            count_in_slice += 1
            if (
                count_in_slice >= self.SLICE_EVENTS
                or time.monotonic() - slice_start >= self.SLICE_BUDGET_S
            ):
                count_in_slice = 0
                slice_start = time.monotonic()
                await asyncio.sleep(0)  # yield the loop: bounded occupancy

    def stats(self) -> dict[str, int]:
        """Queue depth + counters for BOTH queues (dashboard/observability)."""
        return {
            "queued": len(self._queue),
            "control_queued": len(self._control_queue),
            "dispatched": self._dispatched,
            "dropped": self._dropped,
            "control_dropped": self._control_dropped,
            "unattributed_cancels": self._unattributed_cancels,
        }

    async def publish(self, event: Event) -> list[Any]:
        """Invoke all matching handlers (sync or async), isolated per handler.

        Returns handler results / exception objects in invocation order.
        An in-handler CancelledError counts as a handler failure unless the
        publish task itself is being cancelled; KeyboardInterrupt/SystemExit
        always propagate (process-level signals).
        """
        results: list[Any] = []
        key = type(event)
        cells = self._type_cache.get(key)
        if cells is None:  # one-time per (type, subscription-epoch): O(S)
            cells = tuple(h for t, h in self._subs.items() if issubclass(key, t))
            self._type_cache[key] = cells
        for cell in cells:
            for handler in cell.snapshot():
                try:
                    outcome = handler(event)
                    if inspect.iscoroutine(outcome):
                        outcome = await outcome
                    results.append(outcome)
                except asyncio.CancelledError:
                    # read-only check (the old cancelling()>uncancel() idiom
                    # consumed the very cancellation it was inspecting);
                    # a TRUE outer cancel re-raises. On 3.10 (no
                    # Task.cancelling) the cancel is unattributable:
                    # re-raising skipped the remaining subscribers AND
                    # killed the dispatcher (total bus wedge) — isolate
                    # instead (log + record the failure, keep draining).
                    if cancellation_attributable() and outer_cancel_pending(unknown=True):
                        raise
                    if not cancellation_attributable():
                        self._unattributed_cancels += 1
                    LOG.exception(
                        "event handler %r cancelled internally for %s",
                        handler,
                        type(event).__name__,
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
    "CONTROL_PLANE_EVENTS",
    "AccountConnected",
    "AccountDisconnected",
    "AccountFailed",
    "Event",
    "EventBus",
    "FleetStateChanged",
    "MessageDeleted",
    "MessageEdited",
    "MessageReceived",
    "PluginEnabledChanged",
    "PluginLoaded",
    "PluginUnloaded",
    "RateLimitTriggered",
    "ReactionAdded",
    "ScenarioFinished",
    "ScenarioStarted",
    "SettingChanged",
    "TaskCancelled",
    "TaskCompleted",
    "TaskFailed",
    "TaskQueued",
    "TaskStarted",
]
