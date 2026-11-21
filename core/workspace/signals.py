"""SignalOutbox — durable signal delivery (W4; decision 12).

Chain-triggering signals are accepted DURABLY before acknowledgement:
the signal row (or its outbox record) is persisted FIRST, then the
emission returns success. Delivery is at-least-once; consumers
deduplicate by ``signal_id`` and must be idempotent. Queue pressure
may delay delivery but can never discard an accepted signal; invalid
payloads are rejected at emission (the domain model validates) or
recorded as classified terminal errors.

The outbox is a PERSISTENCE + DELIVERY component over the v10
``signals`` table: it does not decide what a signal triggers (that is
the chain runtime's job, through matching against chain bindings).
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any, Protocol

from .models import SignalIdentity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["DEAD_LETTER_MAX_ATTEMPTS", "SignalOutbox", "SignalRecords"]

#: delivery attempts before a signal is dead-lettered (classified
#: terminal failure — retained for inspection, never re-delivered)
DEAD_LETTER_MAX_ATTEMPTS = 5


class SignalRecords(Protocol):
    """The persistence surface over the v10 ``signals`` table."""

    async def insert(self, signal: SignalIdentity) -> None: ...

    async def pending(self, limit: int) -> list[SignalIdentity]: ...

    async def mark_delivered(self, signal_id: str, attempts: int) -> None: ...

    async def mark_dead(self, signal_id: str, attempts: int, error: str) -> None: ...

    async def record_attempt(self, signal_id: str, error: str) -> None: ...

    async def attempts_of(self, signal_id: str) -> int | None: ...

    async def purge_delivered_before(self, cutoff: float) -> int: ...


class SignalOutbox:
    """Accept durably, deliver at-least-once, dedupe by identity."""

    def __init__(
        self,
        records: SignalRecords,
        *,
        deliver: Callable[[SignalIdentity], Awaitable[Any]] | None = None,
        time_fn: Callable[[], float] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._records = records
        self._deliver = deliver
        self._now = time_fn or time.time
        self._new_id = id_factory or (lambda: uuid.uuid4().hex)
        #: in-flight dedupe (the accepted-this-process set): a duplicate
        #: emission of the SAME identity returns the existing id without
        #: a second row (idempotent acceptance)
        self._accepted: dict[str, str] = {}

    async def accept(
        self,
        account: str,
        plugin: str,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str = "",
    ) -> str:
        """THE emission path: persist FIRST, then acknowledge.

        Returns the stable ``signal_id``. Raises InvalidDefinition for
        malformed emissions (bare/namespaced-name violations, non-dict
        payloads) BEFORE anything is persisted — an invalid signal is
        never accepted. Consumers dedupe by the returned id.
        """
        signal = SignalIdentity(
            signal_id=self._new_id(),
            account=account,
            plugin=plugin,
            name=name,
            payload=dict(payload or {}),
            correlation_id=correlation_id,
            created_at=self._now(),
        )
        await self._records.insert(signal)  # durable BEFORE ack
        self._accepted[signal.namespaced] = signal.signal_id
        return signal.signal_id

    async def accepted_id(self, account: str, plugin: str, name: str) -> str | None:
        """The id a (plugin:name) emission received this process, if any
        — the consumer-side dedupe companion."""
        return self._accepted.get(f"{plugin}:{name}")

    def bind_delivery(self, deliver: Callable[[SignalIdentity], Awaitable[Any]]) -> None:
        """Late-bind the delivery callback (bootstrap order: the outbox
        must exist before the chain runtime that consumes it, so the
        constructor takes deliver=None and the runtime's intake binds
        here — found never-wired in production by the live-debugging
        toolkit (2026-11-17); signals pended forever)."""
        self._deliver = deliver

    async def deliver_pending(self, limit: int = 50) -> dict[str, int]:
        """One delivery sweep: every pending signal gets its delivery
        callback; success acks, failure counts an attempt and retries
        later, exhausting into a dead letter. Never raises — the sweep
        is the reconciler's heartbeat."""
        if self._deliver is None:
            return {"delivered": 0, "failed": 0, "dead": 0}
        delivered = failed = dead = 0
        for signal in await self._records.pending(limit):
            try:
                await self._deliver(signal)
            except Exception as exc:
                await self._records.record_attempt(signal.signal_id, str(exc))
                # read-back of the attempt count decides dead-lettering
                # (the record owns the counter; single source of truth)
                attempts = await self._records.attempts_of(signal.signal_id)
                if attempts is not None and attempts >= DEAD_LETTER_MAX_ATTEMPTS:
                    await self._records.mark_dead(signal.signal_id, attempts, str(exc))
                    dead += 1
                else:
                    failed += 1
            else:
                await self._records.mark_delivered(signal.signal_id, 0)
                delivered += 1
        return {"delivered": delivered, "failed": failed, "dead": dead}

    async def purge(self, older_than_s: float = 86400.0) -> int:
        """Retention: delivered signals and dead letters older than the
        window are removed (bounded history)."""
        return await self._records.purge_delivered_before(self._now() - older_than_s)
