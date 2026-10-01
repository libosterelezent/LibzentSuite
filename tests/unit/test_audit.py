"""Unit tests for core.audit: ring, tail filters, DB sink, history, format."""
from __future__ import annotations

import time
import unittest

from core.audit import AuditEntry, AuditRecorder, format_audit


class FakeAuditRepo:
    """Duck-typed AuditRepository (add/recent), fault-togglable."""

    def __init__(self) -> None:
        self.added = []
        self.recent_args = []
        self.rows = []
        self.fail = False

    def add(self, entry):
        if self.fail:
            raise RuntimeError("audit db down")
        self.added.append(dict(entry))
        return len(self.added)

    def recent(self, limit=100, account=None):
        if self.fail:
            raise RuntimeError("audit db down")
        self.recent_args.append((limit, account))
        return [dict(r) for r in self.rows]


class TestRecordAndTail(unittest.TestCase):
    def test_record_returns_json_safe_dict(self) -> None:
        recorder = AuditRecorder()
        entry = recorder.record(
            "alpha", "out", "message", 500, "hello", wait_ms=12.5, jitter_ms=3.25
        )
        self.assertEqual(entry["account"], "alpha")
        self.assertEqual(entry["direction"], "out")
        self.assertEqual(entry["action"], "message")
        self.assertEqual(entry["channel_id"], 500)
        self.assertEqual(entry["content"], "hello")
        self.assertEqual(entry["wait_ms"], 12.5)
        self.assertEqual(entry["jitter_ms"], 3.25)
        self.assertEqual(round(entry["ts"], 3), entry["ts"])  # rounded

    def test_record_defaults(self) -> None:
        recorder = AuditRecorder()
        entry = recorder.record("alpha", "in", "event:message")
        self.assertIsNone(entry["channel_id"])
        self.assertEqual(entry["content"], "")
        self.assertEqual(entry["wait_ms"], 0.0)
        self.assertEqual(entry["jitter_ms"], 0.0)
        self.assertEqual(entry["note"], "")

    def test_ring_capacity_drops_oldest(self) -> None:
        recorder = AuditRecorder(capacity=3)
        for i in range(5):
            recorder.record("alpha", "out", "message", i, f"m{i}")
        tail = recorder.tail(10)
        self.assertEqual([e["content"] for e in tail], ["m2", "m3", "m4"])

    def test_tail_filter_by_account(self) -> None:
        recorder = AuditRecorder()
        recorder.record("alpha", "out", "message", 500, "hello", wait_ms=12.5)
        recorder.record("beta", "in", "event:message", 500, "hi")
        recorder.record("alpha", "out", "dm", 900, "page", jitter_ms=800)
        alpha = recorder.tail(10, account="alpha")
        self.assertEqual([e["action"] for e in alpha], ["message", "dm"])
        self.assertEqual(recorder.tail(10)[0]["direction"], "out")

    def test_tail_nonpositive_n_returns_empty(self) -> None:
        recorder = AuditRecorder()
        recorder.record("alpha", "out", "message", 500, "x")
        self.assertEqual(recorder.tail(0), [])
        self.assertEqual(recorder.tail(-3), [])

    def test_tail_respects_limit(self) -> None:
        recorder = AuditRecorder()
        for i in range(4):
            recorder.record("alpha", "out", "message", i, f"m{i}")
        self.assertEqual([e["content"] for e in recorder.tail(2)], ["m2", "m3"])


class TestDbSink(unittest.TestCase):
    def test_best_effort_db_write(self) -> None:
        repo = FakeAuditRepo()
        recorder = AuditRecorder(db_repo=repo)
        recorder.record("alpha", "out", "message", 500, "stored")
        self.assertEqual(len(repo.added), 1)
        self.assertEqual(repo.added[0]["content"], "stored")

    def test_db_sink_failure_never_breaks_recording(self) -> None:
        class Bomber:
            def add(self, entry):
                raise RuntimeError("db down")

        recorder = AuditRecorder(db_repo=Bomber())
        entry = recorder.record("alpha", "out", "message", 500, "still fine")
        self.assertEqual(entry["content"], "still fine")
        self.assertEqual(len(recorder.tail(1)), 1)

    def test_history_from_repo_drops_id_and_rounds_ts(self) -> None:
        repo = FakeAuditRepo()
        repo.rows = [
            {"id": 7, "ts": 1700000000.123456, "account": "alpha",
             "direction": "out", "action": "message", "channel_id": 5,
             "content": "db row", "wait_ms": 10.0, "jitter_ms": 2.0, "note": ""}
        ]
        recorder = AuditRecorder(db_repo=repo)
        rows = recorder.history(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("id", rows[0])
        self.assertEqual(rows[0]["ts"], round(1700000000.123456, 3))

    def test_history_passes_limit_and_account_to_repo(self) -> None:
        repo = FakeAuditRepo()
        recorder = AuditRecorder(db_repo=repo)
        recorder.history(limit=17, account="alpha")
        self.assertEqual(repo.recent_args, [(17, "alpha")])

    def test_history_falls_back_to_ring_when_repo_fails(self) -> None:
        repo = FakeAuditRepo()
        recorder = AuditRecorder(db_repo=repo)
        recorder.record("alpha", "out", "message", 500, "ring row")
        repo.fail = True
        rows = recorder.history()
        self.assertEqual([r["content"] for r in rows], ["ring row"])

    def test_history_without_repo_uses_ring(self) -> None:
        recorder = AuditRecorder()
        recorder.record("alpha", "out", "message", 500, "a")
        recorder.record("beta", "out", "message", 501, "b")
        self.assertEqual([r["content"] for r in recorder.history(account="alpha")], ["a"])


class TestSubscriptions(unittest.TestCase):
    def test_subscribe_and_unsubscribe(self) -> None:
        recorder = AuditRecorder()
        seen = []
        unsubscribe = recorder.subscribe(seen.append)
        recorder.record("alpha", "out", "message", 500, "x")
        self.assertEqual(len(seen), 1)
        unsubscribe()
        unsubscribe()  # idempotent
        recorder.record("alpha", "out", "message", 500, "y")
        self.assertEqual(len(seen), 1)

    def test_bad_subscriber_is_isolated(self) -> None:
        recorder = AuditRecorder()
        seen = []
        recorder.subscribe(lambda e: 1 / 0)
        recorder.subscribe(seen.append)
        recorder.record("alpha", "in", "event:message", 500, "z")
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(recorder.tail(1)), 1)  # entry stored despite the crash


class TestFormatAudit(unittest.TestCase):
    TS = 1700000000.5  # fixed timestamp -> deterministic clock rendering

    def test_outbound_line_shape(self) -> None:
        lines = format_audit([
            {"ts": self.TS, "account": "alpha", "direction": "out", "action": "message",
             "channel_id": 123, "content": "hi there", "wait_ms": 100, "jitter_ms": 40,
             "note": "plugin=p task=1"}
        ])
        self.assertEqual(len(lines), 1)
        clock = time.strftime("%H:%M:%S", time.localtime(self.TS))
        self.assertTrue(lines[0].startswith(clock))
        self.assertIn("-> alpha", lines[0])
        self.assertIn("message", lines[0])
        self.assertIn("ch=123", lines[0])
        self.assertIn("hi there", lines[0])
        self.assertIn("(wait 100ms, jitter 40ms)", lines[0])
        self.assertIn("plugin=p task=1", lines[0])

    def test_inbound_arrow_and_missing_channel(self) -> None:
        lines = format_audit([
            {"ts": self.TS, "account": "alpha", "direction": "in",
             "action": "event:message", "channel_id": None, "content": "yo"}
        ])
        self.assertIn("<- alpha", lines[0])
        self.assertIn("ch=-", lines[0])
        self.assertNotIn("wait", lines[0])  # zero waits are omitted

    def test_preview_escapes_newlines_and_truncates(self) -> None:
        lines = format_audit([
            {"ts": self.TS, "account": "a", "direction": "out", "action": "message",
             "channel_id": 1, "content": "line1\nline2"}
        ])
        self.assertIn("line1\\nline2", lines[0])
        long = "x" * 100
        lines = format_audit([
            {"ts": self.TS, "account": "a", "direction": "out", "action": "message",
             "channel_id": 1, "content": long}
        ])
        self.assertNotIn(long, lines[0])
        self.assertIn("...", lines[0])
        self.assertLess(len(lines[0]), 140)

    def test_accepts_audit_entry_objects(self) -> None:
        lines = format_audit([
            AuditEntry(ts=self.TS, account="alpha", direction="out", action="dm",
                       channel_id=9, content="hey", wait_ms=5.0, jitter_ms=0.0)
        ])
        self.assertIn("-> alpha", lines[0])
        self.assertIn("(wait 5ms)", lines[0])

    def test_empty_placeholder(self) -> None:
        self.assertEqual(format_audit([]), ["(no interactions recorded)"])


class TestConfig(unittest.TestCase):
    def test_capacity_validation(self) -> None:
        with self.assertRaises(ValueError):
            AuditRecorder(capacity=0)
        with self.assertRaises(ValueError):
            AuditRecorder(capacity=True)
        AuditRecorder(capacity=1)  # smallest legal ring


if __name__ == "__main__":
    unittest.main()
