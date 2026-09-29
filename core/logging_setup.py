"""Logging setup: one namespaced logger tree ("mudae.*") + live tail buffer."""
from __future__ import annotations

import logging
import sys
from collections import deque
from typing import Callable, Deque, List, Optional

ROOT_LOGGER = "mudae"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
TAIL_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
TAIL_DATEFMT = "%H:%M:%S"


def setup_logging(level: int = logging.INFO, stream=None) -> logging.Logger:
    """Configure the 'mudae' logger tree; idempotent.

    Expects: optional stdlib level and output stream (default stderr).
    Returns: the root 'mudae' logger with a single stream handler attached.
    """
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def plugin_logger(account: str, plugin: str) -> logging.Logger:
    """Logger for a plugin running on a specific account: mudae.<account>.<plugin>."""
    return logging.getLogger(f"{ROOT_LOGGER}.{account}.{plugin}")


class RingBufferHandler(logging.Handler):
    """Keeps the last N formatted log lines + pushes live subscribers.

    Used by the TUI log pane and the dashboard WebSocket log stream. Safe to
    subscribe from any thread; callbacks run on the emitting thread.
    """

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.lines: Deque[str] = deque(maxlen=capacity)
        self._subscribers: List[Callable[[str], None]] = []
        self.setFormatter(logging.Formatter(TAIL_FORMAT, datefmt=TAIL_DATEFMT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # never let logging formatting kill the app
            line = f"<unformattable {record.levelname} record: {record.name}>"
        self.lines.append(line)
        for callback in list(self._subscribers):
            try:
                callback(line)
            except Exception:
                pass

    def tail(self, n: int = 50) -> List[str]:
        """Most recent n lines, oldest first."""
        return list(self.lines)[-max(0, n):]

    def subscribe(self, callback: Callable[[str], None]) -> Callable[[], None]:
        """Register a live-tail callback; returns an unsubscribe callable."""
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return _unsubscribe


_SHARED_RING: Optional[RingBufferHandler] = None


def attach_ring_buffer(capacity: int = 500, level: int = logging.INFO) -> RingBufferHandler:
    """Attach (or return the existing) shared ring buffer to the mudae tree."""
    global _SHARED_RING
    if _SHARED_RING is None:
        _SHARED_RING = RingBufferHandler(capacity)
        _SHARED_RING.setLevel(level)
        logging.getLogger(ROOT_LOGGER).addHandler(_SHARED_RING)
    return _SHARED_RING
