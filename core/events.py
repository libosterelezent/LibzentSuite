"""Event bus: routes Discord events from sessions to subscribers (plugins).

Guarantees:
  * One failing handler never breaks other handlers (isolation).
  * Subscribers registered for a base class receive subclass events (isinstance dispatch).
  * publish() can be awaited (sequential results) or fired via publish_nowait().
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Type

LOG = logging.getLogger("slaughtermudae.events")

Handler = Callable[["Event"], Any]


@dataclass
class Event:
    """Base event. ts defaults to wall clock at creation."""

    account: str
    ts: float = field(default_factory=time.time)

    @property
    def name(self) -> str:
        return type(self).__name__


@dataclass
class ReadyEvent(Event):
    """Emitted after an account session connected successfully."""


@dataclass
class MessageEvent(Event):
    """A Discord message seen by an account (inbound or the account's own sends)."""

    message_id: int = 0
    channel_id: int = 0
    author_id: int = 0
    content: str = ""
    dm: bool = False


@dataclass
class ReactionEvent(Event):
    """A reaction added (reserved for future plugins)."""

    message_id: int = 0
    channel_id: int = 0
    user_id: int = 0
    emoji: str = ""


class EventBus:
    """Minimal async pub/sub with subclass-aware dispatch and error isolation."""

    def __init__(self) -> None:
        self._subs: Dict[Type[Event], List[Handler]] = {}

    def subscribe(self, event_type: Type[Event], handler: Handler) -> Callable[[], None]:
        """Register handler for event_type (and subclasses).

        Returns an unsubscribe callable.
        """
        self._subs.setdefault(event_type, []).append(handler)

        def _unsubscribe() -> None:
            try:
                self._subs.get(event_type, []).remove(handler)
            except ValueError:
                pass

        return _unsubscribe

    def publish_nowait(self, event: Event) -> asyncio.Task:
        """Fire-and-forget publish; safe to call from sync callbacks inside a loop."""
        return asyncio.get_running_loop().create_task(self.publish(event))

    async def publish(self, event: Event) -> List[Any]:
        """Invoke all matching handlers (sync or async) in subscription order.

        Exceptions in a handler are logged and recorded in the results list
        (instance of the exception) instead of propagating.
        Returns: handler results / exception objects, in invocation order.
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
                except Exception as exc:  # isolation by design
                    LOG.exception("event handler %r failed for %s", handler, event.name)
                    results.append(exc)
        return results

    def subscriber_count(self, event_type: Optional[Type[Event]] = None) -> int:
        """Number of subscriptions, optionally filtered to one event type."""
        if event_type is not None:
            return len(self._subs.get(event_type, []))
        return sum(len(h) for h in self._subs.values())
