"""Audit trail: outbound/inbound recording, waits, delays, ring + history."""
from __future__ import annotations

import asyncio
import unittest

from core.audit import AuditRecorder, format_audit

from .helpers import TEST_CHANNEL, Harness


def fast_rules() -> dict:
    return {"message": "3/1s", "dm": "3/1s"}


class TestAuditRecorder(unittest.TestCase):
    def test_record_tail_and_filter(self) -> None:
        recorder = AuditRecorder()
        recorder.record("alpha", "out", "message", 500, "hello", wait_ms=12.5)
        recorder.record("beta", "in", "event:message", 500, "hi")
        recorder.record("alpha", "out", "dm", 900, "page", delay_ms=800)
        alpha = recorder.tail(10, account="alpha")
        self.assertEqual([e.action for e in alpha], ["message", "dm"])
        self.assertEqual(recorder.tail(10)[0].direction, "out")

    def test_live_subscription(self) -> None:
        recorder = AuditRecorder()
        seen: list = []
        unsubscribe = recorder.subscribe(seen.append)
        recorder.record("alpha", "out", "message", 500, "x")
        self.assertEqual(len(seen), 1)
        unsubscribe()
        recorder.record("alpha", "out", "message", 500, "y")
        self.assertEqual(len(seen), 1)

    def test_bad_subscriber_is_isolated(self) -> None:
        recorder = AuditRecorder()
        seen: list = []
        recorder.subscribe(lambda e: 1 / 0)
        recorder.subscribe(seen.append)
        recorder.record("alpha", "in", "event:message", 500, "z")
        self.assertEqual(len(seen), 1)

    def test_format_audit_lines(self) -> None:
        recorder = AuditRecorder()
        recorder.record("alpha", "out", "message", TEST_CHANNEL, "hi there", wait_ms=100, delay_ms=50)
        lines = format_audit(recorder.tail(5))
        self.assertIn("-> alpha", lines[0])
        self.assertIn("wait 100ms", lines[0])
        self.assertIn("delay 50ms", lines[0])
        self.assertEqual(format_audit([]), ["  (no interactions recorded)"])

    def test_db_sink_failure_never_breaks_recording(self) -> None:
        class Bomber:
            def add_audit(self, entry):
                raise RuntimeError("db down")

        recorder = AuditRecorder(database=Bomber())
        recorder.record("alpha", "out", "message", TEST_CHANNEL, "still fine")
        self.assertEqual(len(recorder.tail(1)), 1)


class TestSessionAuditWiring(unittest.IsolatedAsyncioTestCase):
    async def test_outbound_sends_recorded_with_wait_and_delay(self) -> None:
        recorder = AuditRecorder()
        harness = Harness(
            "tests/fixtures/plugins", ["alpha"], rate_rules=fast_rules(), audit=recorder
        )
        await harness.container.start_account("alpha")
        session = harness.session("alpha")
        harness.limiter.set_delay("message", 0.05, 0.06)  # tiny human delay
        await session.send_message(TEST_CHANNEL, "one")
        await session.send_message(TEST_CHANNEL, "two")
        await session.send_message(TEST_CHANNEL, "three")
        await session.send_message(TEST_CHANNEL, "four")  # bucket empty -> wait
        outbound = [e for e in recorder.tail(20) if e.direction == "out"]
        self.assertEqual([e.content for e in outbound], ["one", "two", "three", "four"])
        self.assertTrue(all(e.delay_ms >= 40 for e in outbound))          # delay applied
        waits = [e.wait_ms for e in outbound]
        self.assertGreater(waits[-1], waits[0])                          # 4th waited longest
        await harness.container.stop_account("alpha")

    async def test_inbound_events_recorded(self) -> None:
        recorder = AuditRecorder()
        harness = Harness("tests/fixtures/plugins", ["alpha"], audit=recorder)
        await harness.container.start_account("alpha")
        harness.world.deliver(TEST_CHANNEL, harness.owner_id, "hello")
        await asyncio.sleep(0.05)
        inbound = [e for e in recorder.tail(10) if e.direction == "in"]
        self.assertEqual(len(inbound), 1)
        self.assertEqual(inbound[0].content, "hello")
        self.assertIn("author=", inbound[0].note)
        await harness.container.stop_account("alpha")

    async def test_no_recorder_means_no_crash(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"], audit=None)
        await harness.container.start_account("alpha")
        session = harness.session("alpha")
        await session.send_message(TEST_CHANNEL, "silent")
        await harness.container.stop_account("alpha")


if __name__ == "__main__":
    unittest.main()
