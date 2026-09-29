"""Ring buffer log handler: tail, live subscription, capacity trim."""
from __future__ import annotations

import logging
import unittest

from core.logging_setup import RingBufferHandler, attach_ring_buffer


class TestRingBufferHandler(unittest.TestCase):
    def test_tail_returns_emitted_lines(self) -> None:
        ring = RingBufferHandler(capacity=10)
        logger = logging.getLogger("slaughtermudae.test.ring1")
        logger.addHandler(ring)
        logger.setLevel(logging.INFO)
        logger.info("hello %s", "world")
        logger.warning("careful")
        tail = ring.tail(10)
        self.assertEqual(len(tail), 2)
        self.assertIn("hello world", tail[0])
        self.assertIn("careful", tail[1])
        self.assertIn("WARNING", tail[1])
        logger.removeHandler(ring)

    def test_capacity_trims_oldest(self) -> None:
        ring = RingBufferHandler(capacity=3)
        logger = logging.getLogger("slaughtermudae.test.ring2")
        logger.addHandler(ring)
        logger.setLevel(logging.INFO)
        for i in range(6):
            logger.info("line-%d", i)
        tail = ring.tail(10)
        self.assertEqual([line.split("line-")[1] for line in tail], ["3", "4", "5"])
        logger.removeHandler(ring)

    def test_live_subscription_and_unsubscribe(self) -> None:
        ring = RingBufferHandler()
        seen: list[str] = []
        unsubscribe = ring.subscribe(seen.append)
        ring.emit(logging.LogRecord("x", logging.INFO, "p", 1, "pushed", None, None))
        self.assertEqual(len(seen), 1)
        self.assertIn("pushed", seen[0])
        unsubscribe()
        ring.emit(logging.LogRecord("x", logging.INFO, "p", 1, "ignored", None, None))
        self.assertEqual(len(seen), 1)

    def test_bad_subscriber_is_isolated(self) -> None:
        ring = RingBufferHandler()
        ring.subscribe(lambda line: 1 / 0)  # noqa: B023 — raises on emit
        seen: list[str] = []
        ring.subscribe(seen.append)
        ring.emit(logging.LogRecord("x", logging.INFO, "p", 1, "survives", None, None))
        self.assertEqual(len(seen), 1)

    def test_attach_ring_buffer_is_shared(self) -> None:
        first = attach_ring_buffer()
        second = attach_ring_buffer()
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
