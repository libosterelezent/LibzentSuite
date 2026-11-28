"""expect — the send-coupled reactive window (client-capability Track B).

The flaw this primitive closes (owner review 2026-11-17): a
``wait_for`` issued AFTER a paced send misses a reply that lands
between the send's ack and the subscription — the event fired to no
listener — and pre-creating the waiter before the send dangles an
irrelevant task while inverting the code's shape. The window inverts
it back: entering subscribes BEFORE the send runs, so a reply landing
during the send's queue wait buffers exactly like one landing after
(design: docs/plans/client-capability-adoption.md, Track B; the
Mudabot comparison lives in the sibling design reference).

Semantics (the contract plugin authors rely on):

* ONE subscription — opened at ``__aenter__``, unwired at
  ``__aexit__``. The framework owns the waiter: a parked await is
  released at exit, so no plugin-authored waiter task dangles.
* ONE deadline — ``timeout`` is a total budget from entry shared by
  every await. It bounds how long an await will PARK, not what the
  window can see: events keep buffering until the block exits, and a
  buffered match is returned even after the deadline lapsed.
* A bounded ring of the account's inbound events since entry plus a
  consuming cursor: sequential awaits return distinct events in
  arrival order, and ``first_of`` applies checks the window was not
  opened with. Under a flood beyond ``RING_MAX`` unconsumed events the
  oldest are evicted — matching degrades honestly, memory stays
  bounded.
* Checks see the ``MessageReceived`` event (flat fields plus the
  parsed ``.message`` rich model — embeds and components are parsed
  once at the adapter boundary). The rich model is frozen; the event
  itself is a shared plain dataclass — checks must not mutate it, the
  same object reaches later consumers).
* A raising check surfaces as :class:`core.errors.AccountError` from
  the awaiting call with the cause attached — the same contract as
  ``wait_for_message`` / ``collect_messages``.
* An await on a window that is not open (never entered, or exited)
  raises ``RuntimeError`` — a stale window fails loud, never None.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from .errors import AccountError
from .event_bus import MessageReceived

if TYPE_CHECKING:
    from types import TracebackType

__all__ = ["ExpectWindow"]

#: the subscribe seam the facade hands the window: register one
#: per-account message waiter, return its idempotent unsubscribe. The
#: wired path routes through MessageWaiterRegistry (per-account
#: dispatch, M/A fan-out); the fallback path is the raw bus
#: subscription (direct-construction tests, unwired stacks).
WaitSubscribe = Callable[[Callable[[MessageReceived], None]], Callable[[], None]]


class ExpectWindow:
    """A send-coupled reactive window over one account's inbound stream.

    Built only by :meth:`AccountPluginServices.expect`; plugins never
    construct it. The canonical shape (the send rides INSIDE the
    window):

        async with ctx.services.expect(check=my_check, timeout=30) as window:
            await ctx.services.send_message(channel_id, "$wa")
            reply = await window.match()                # first match wins
            pages = await window.collect(n=2)           # same window, more matches
            either = await window.first_of(is_a, is_b)  # alternative checks

    Entering subscribes BEFORE the send runs; the reply may land at
    any moment after entry and still match. The window is
    single-consumer by design: concurrent awaits serialize on an
    internal lock so the consuming cursor stays deterministic.
    """

    #: inbound events retained while unconsumed. A flood beyond this
    #: evicts the oldest: matching degrades honestly (bounded memory,
    #: no crash). A window whose channel floods 256-deep unconsumed
    #: has lost its flow either way — the trade favors the bound.
    RING_MAX: ClassVar[int] = 256

    def __init__(
        self,
        *,
        account: str,
        plugin: str,
        check: Callable[[MessageReceived], bool],
        timeout: float,
        subscribe: WaitSubscribe,
    ) -> None:
        self._account = account
        self._plugin = plugin
        self._check = check
        self._timeout = float(timeout)
        self._subscribe = subscribe
        #: consuming cursor — absolute event index one past the last
        #: examined event (events before it are this window's past)
        self._consumed = 0
        #: absolute count of events appended since entry — ring
        #: indices shift on eviction, absolute counts never do
        self._appended = 0
        self._ring: collections.deque[MessageReceived] = collections.deque(maxlen=self.RING_MAX)
        self._unsub: Callable[[], None] | None = None
        self._deadline: float | None = None
        self._closed = False
        # loop-bound primitives: expect() runs on the plugin's loop,
        # the loop the window lives and parks on
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> ExpectWindow:
        if self._closed:
            raise RuntimeError("expect window cannot be re-entered after close")
        if self._unsub is not None:
            raise RuntimeError("an expect window is already open")
        # THE order that closes the wait_for race: the subscription
        # goes live BEFORE any send issued inside the with-block.
        self._unsub = self._subscribe(self._on_event)
        self._deadline = asyncio.get_running_loop().time() + self._timeout
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # fully synchronous (no awaits): a consumer waking mid-teardown
        # never observes a half-closed window
        self._closed = True
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        # release parked consumers — they observe the closed state and
        # raise; a plugin's abandoned waiter completes, never dangles
        self._wake.set()

    def _on_event(self, event: MessageReceived) -> None:
        if self._closed or self._unsub is None:
            return  # late dispatch racing the unwire: drop
        if event.account != self._account:
            return  # the unfiltered bus path delivers other accounts too
        self._ring.append(event)
        self._appended += 1
        self._wake.set()

    # -------------------------------------------------------------- awaiting
    async def match(self) -> MessageReceived | None:
        """The first unconsumed event passing the window's check.

        ``None`` when the deadline lapses without a match. The
        returned event is consumed: a later await sees only later
        events.
        """
        return await self._first_passing((self._check,))

    async def first_of(self, *checks: Callable[[MessageReceived], bool]) -> MessageReceived | None:
        """The first unconsumed event passing ANY of ``checks``.

        The window's own check is not consulted — alternative
        predicates over the same buffered stream. Consumes the
        returned event.
        """
        if not checks:
            raise AccountError("first_of needs at least one check")
        return await self._first_passing(checks)

    async def collect(self, n: int) -> list[MessageReceived]:
        """Gather up to ``n`` events passing the window's check.

        Returns what was gathered when the deadline lapses — a partial
        (or empty) list is a normal outcome; callers judge sufficiency
        by ``len()``. Returned events are consumed; non-matching
        events in between are examined and passed over.
        """
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise AccountError(f"collect n must be a positive int, got {n!r}")
        matches: list[MessageReceived] = []
        async with self._lock:
            while len(matches) < n:
                self._ensure_open()
                self._scan_collect(matches, n)
                if len(matches) >= n:
                    break
                if self._expired():
                    break
                await self._park()
            return matches

    async def _first_passing(
        self, checks: tuple[Callable[[MessageReceived], bool], ...]
    ) -> MessageReceived | None:
        async with self._lock:
            while True:
                self._ensure_open()
                found = self._scan_first(checks)
                if found is not None:
                    return found
                if self._expired():
                    return None
                await self._park()

    # --------------------------------------------------------------- scanning
    def _scan_first(
        self, checks: tuple[Callable[[MessageReceived], bool], ...]
    ) -> MessageReceived | None:
        """Scan from the cursor; return the first passing event with
        the cursor advanced past it (``None`` still advances it past
        everything examined — non-matching events are this window's
        past too)."""
        evicted = self._evicted()
        start = max(self._consumed, evicted)
        while start < self._appended:
            event = self._ring[start - evicted]
            start += 1
            if self._passes(checks, event):
                self._consumed = start
                return event
        self._consumed = start
        return None

    def _scan_collect(self, matches: list[MessageReceived], n: int) -> None:
        """Gather up to ``n`` matches from the cursor, advancing it
        past everything examined."""
        checks = (self._check,)  # once per scan, not per event
        evicted = self._evicted()
        start = max(self._consumed, evicted)
        while start < self._appended and len(matches) < n:
            event = self._ring[start - evicted]
            start += 1
            if self._passes(checks, event):
                matches.append(event)
        self._consumed = start

    def _passes(
        self, checks: tuple[Callable[[MessageReceived], bool], ...], event: MessageReceived
    ) -> bool:
        """Evaluate one event against the checks (OR-ed together).

        A raising check surfaces as :class:`AccountError` with the
        cause attached — the same contract the wait family carries —
        and the cursor does not advance past the poisoned event: it
        stays unconsumed and every later await reports the same
        failure.
        """
        try:
            return any(check(event) for check in checks)
        except Exception as exc:
            raise AccountError(
                f"expect check failed on account {self._account!r}"
                f" plugin {self._plugin!r}: {type(exc).__name__}: {exc}"
            ) from exc

    def _evicted(self) -> int:
        """Absolute index of the oldest retained event (flood clamp)."""
        return self._appended - len(self._ring)

    # ---------------------------------------------------------------- timing
    def _remaining(self) -> float:
        """Seconds of budget left (0 once the deadline lapsed)."""
        if self._deadline is None:
            return 0.0
        return max(0.0, self._deadline - asyncio.get_running_loop().time())

    def _expired(self) -> bool:
        return self._remaining() <= 0.0

    async def _park(self) -> None:
        """Wait for the next event, the deadline, or the window close.

        Whichever fires, the caller re-scans: a wake lost to the clear
        below cannot lose a match, because the ring — not the wake —
        is the record.
        """
        self._wake.clear()
        # a timeout parks the caller into its rescan; the ring never
        # lies about what arrived while we slept
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), self._remaining())

    def _ensure_open(self) -> None:
        if self._closed or self._unsub is None:
            raise RuntimeError(
                "expect window awaits are valid only inside the"
                f" async-with block (account {self._account!r}"
                f" plugin {self._plugin!r})"
            )

    def __repr__(self) -> str:
        state = "unentered"
        if self._unsub is not None:
            state = "open"
        elif self._closed:
            state = "closed"
        return (
            f"<ExpectWindow {self._account!r}/{self._plugin!r} {state}"
            f" appended={self._appended} consumed={self._consumed}>"
        )
