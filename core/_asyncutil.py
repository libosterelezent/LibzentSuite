"""Small asyncio helpers shared by the event bus and the task scheduler."""
from __future__ import annotations

import asyncio
from typing import Optional


def cancellation_attributable(task: Optional["asyncio.Task"] = None) -> bool:
    """True when this interpreter can TELL an outer ``Task.cancel()`` apart
    from a handler that merely raised ``CancelledError`` internally.

    That distinction needs ``Task.cancelling`` (Python 3.11+). On 3.10 the
    answer is "no": ``outer_cancel_pending(unknown=...)`` cannot attribute
    the cancellation, and callers must pick a fail-safe strategy
    (isolation + continue, or re-raise) instead of guessing.
    """
    task = task or asyncio.current_task()
    if task is None:
        return True  # no task context: nothing can be pending
    return getattr(task, "cancelling", None) is not None


def outer_cancel_pending(task: Optional["asyncio.Task"] = None, *,
                          unknown: bool = False) -> bool:
    """True when ``task`` (default: the current one) has a PENDING cancel
    request from outside (``Task.cancel()``), as opposed to a handler that
    merely raised ``CancelledError`` by itself.

    READ-ONLY. The idiom this replaces, ``cancelling() > uncancel()``,
    *consumed* one cancellation request every time it ran (``uncancel()``
    decrements the counter), which silently breaks ``asyncio.timeout`` /
    ``TaskGroup`` and can swallow a real cancel; it also raised on Python
    3.10, which has no ``Task.cancelling``.

    ``unknown`` is the answer on interpreters that cannot tell (3.10).
    """
    task = task or asyncio.current_task()
    if task is None:
        return False
    cancelling = getattr(task, "cancelling", None)
    if cancelling is None:
        return unknown
    try:
        return cancelling() > 0
    except Exception:  # pragma: no cover - defensive
        return unknown


__all__ = ["outer_cancel_pending", "cancellation_attributable"]
