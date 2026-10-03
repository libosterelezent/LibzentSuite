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
    burst-limited — today's unbounded ``_background`` growth under a
    message flood becomes a bounded, observable degradation.
    """

    #: queue bound — headroom above the 10k PersistQueue cap (messages are
    #: the flood class); at ~150µs/event fully-loaded this is ~1.5s of
    #: sustained maximal burst before drops begin.
    MAX_QUEUE = 20_000
    #: dispatch slice: events per loop turn before yielding
    SLICE_EVENTS = 128
    #: dispatch slice: wall-seconds per loop turn before yielding
    SLICE_BUDGET_S = 0.004

    def __init__(self) -> None:
        self._subs: Dict[Type[Event], List[Handler]] = {}
        # strong refs for fire-and-forget tasks (documented create_task hazard)
        self._background: set = set()
        self._queue: "collections.deque[Event]" = collections.deque()
        self._dispatcher: Optional["asyncio.Task[None]"] = None
        self._dropped = 0
        self._dispatched = 0
        self._drop_log_gate = 0.0

    def subscribe(self, event_type: Type[Event], handler: Handler) -> Callable[[], None]:
        """Register handler for event_type (and its subclasses).

        Returns an unsubscribe callable (idempotent).
        """
        self._subs.setdefault(event_type, []).append(handler)

        def _unsubscribe() -> None:
            try:
                self._subs.get(event_type, []).remove(handler)
            except ValueError:
                pass

        return _unsubscribe

    async def drain(self, timeout: float = 2.0) -> None:
        """Await the fire-and-forget dispatch backlog (shutdown tail).

        publish_nowait defers dispatch to the coalesced dispatcher; at
        shutdown those orphans would be cancelled by loop close — this
        makes tail-event delivery (AccountDisconnected etc.) contractual,
        not lucky. Re-checks the queue until stable (a handler publishing
        during the drain is flushed by the same call).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            pending = [t for t in list(self._background) if not t.done()]
            if self._dispatcher is not None and not self._dispatcher.done():
                pending.append(self._dispatcher)
            if not pending and not self._queue:
                return
            if pending:
                await asyncio.wait(pending,
                                   timeout=max(0.0, deadline - loop.time()))
            else:
                await asyncio.sleep(0)
            if loop.time() >= deadline:
                if self._queue:
                    LOG.warning(
                        "bus drain timeout: %d events undelivered, "
                        "%d dropped-total", len(self._queue), self._dropped)
                return

    def publish_nowait(self, event: Event) -> Optional["asyncio.Task[None]"]:
        """Fire-and-forget publish; safe from sync callbacks inside a loop.

        Returns the shared dispatcher task (or None on drop). No caller
        consumes the return value (verified); the annotation change from
        Task[List[Any]] is interface-level only.
        """
        if len(self._queue) >= self.MAX_QUEUE:
            self._dropped += 1
            now = time.monotonic()
            if now - self._drop_log_gate > 1.0:  # burst-limited logging
                self._drop_log_gate = now
                LOG.error(
                    "event bus overflow: dropped %s (queued=%d, "
                    "dropped_total=%d)", type(event).__name__,
                    len(self._queue), self._dropped)
            return self._dispatcher
        self._queue.append(event)
        if self._dispatcher is None or self._dispatcher.done():
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
        """
        results: List[Any] = []
        for event_type, handlers in list(self._subs.items()):
            if not isinstance(event, event_type):
                continue
            for handler in list(handlers):
                try:
                    outcome = handler(event)
                    if asyncio.iscoroutine(outcome):
                        outcome = await outcome
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
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:  # isolation by design
                    LOG.exception("event handler %r failed for %s", handler, type(event).__name__)
                    results.append(exc)
        return results

    def subscriber_count(self, event_type: Optional[Type[Event]] = None) -> int:
        """Number of subscriptions, optionally filtered to one event type."""
        if event_type is not None:
            return len(self._subs.get(event_type, []))
        return sum(len(handlers) for handlers in self._subs.values())


__all__ = [
    "EventBus", "Event", "AccountConnected", "AccountDisconnected", "AccountFailed",
    "PluginLoaded", "PluginUnloaded", "PluginEnabledChanged", "TaskQueued",
    "TaskStarted", "TaskCompleted", "TaskFailed", "TaskCancelled",
    "RateLimitTriggered", "SettingChanged", "MessageReceived",
    "MessageEdited", "MessageDeleted", "ReactionAdded",
    "ScenarioStarted", "ScenarioFinished",
]
