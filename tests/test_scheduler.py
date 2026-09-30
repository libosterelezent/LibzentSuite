"""TaskScheduler: interval jobs, persistence, containment."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from core.scheduler import Job, SchedulerError, TaskScheduler


def make_db(tmp: str):
    from core.database import Database

    return Database(Path(tmp) / "s.db")


class TestJobValidation(unittest.TestCase):
    def test_interval_floor_enforced_by_scheduler(self) -> None:
        scheduler = TaskScheduler()  # production floor: 5s
        with self.assertRaises(SchedulerError) as ctx:
            scheduler.add_job("j", "a", 1, "hi", interval=2.0)
        self.assertIn("minimum", str(ctx.exception))

    def test_scheduler_min_interval_relaxable_for_tests(self) -> None:
        scheduler = TaskScheduler(min_interval=0.0)
        job = scheduler.add_job("j", "a", 1, "hi", interval=0.2)
        self.assertEqual(job.interval, 0.2)

    def test_negative_jitter_rejected(self) -> None:
        scheduler = TaskScheduler(min_interval=0.0)
        with self.assertRaises(SchedulerError):
            scheduler.add_job("j", "a", 1, "hi", interval=10, jitter=-1)

    def test_missing_fields_rejected(self) -> None:
        scheduler = TaskScheduler(min_interval=0.0)
        with self.assertRaises(SchedulerError):
            scheduler.add_job("j", "", 1, "hi", interval=10)


class TestSchedulerLoop(unittest.IsolatedAsyncioTestCase):
    async def test_jobs_fire_through_send_hook(self) -> None:
        sent: list[tuple] = []

        async def send(account, channel_id, command):
            sent.append((account, channel_id, command))

        scheduler = TaskScheduler(send=send, tick=0.05, seed=1, min_interval=0.0)
        scheduler.add_job("j1", "alpha", 500, "$wa", interval=0.2, jitter=0.0)
        scheduler.start()
        await asyncio.sleep(0.65)
        await scheduler.stop()
        self.assertGreaterEqual(len(sent), 2)
        self.assertTrue(all(a == "alpha" and c == 500 for a, c, _ in sent))

    async def test_failure_contained_and_recorded(self) -> None:
        async def send(account, channel_id, command):
            raise RuntimeError("account stopped")

        scheduler = TaskScheduler(send=send, tick=0.05, seed=1, min_interval=0.0)
        scheduler.add_job("j1", "alpha", 500, "$wa", interval=0.2)
        scheduler.start()
        await asyncio.sleep(0.55)
        await scheduler.stop()
        job = scheduler.jobs()[0]
        self.assertGreater(job["run_count"], 0)
        self.assertIn("account stopped", job["last_error"] or "")

    async def test_disabled_job_skipped(self) -> None:
        sent: list[tuple] = []

        async def send(account, channel_id, command):
            sent.append(command)

        scheduler = TaskScheduler(send=send, tick=0.05, seed=1, min_interval=0.0)
        scheduler.add_job("j1", "alpha", 500, "$wa", interval=0.2)
        scheduler.set_enabled("j1", False)
        scheduler.start()
        await asyncio.sleep(0.5)
        await scheduler.stop()
        self.assertEqual(sent, [])


class TestPersistence(unittest.TestCase):
    def test_jobs_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(tmp)
            scheduler = TaskScheduler(database=db)
            scheduler.add_job("daily", "alpha", 500, "$wa", interval=30.0, jitter=5.0)
            scheduler.set_enabled("daily", False)
            reloaded = TaskScheduler(database=make_db(tmp))
            jobs = {j["name"]: j for j in reloaded.jobs()}
            self.assertFalse(jobs["daily"]["enabled"])
            self.assertEqual(jobs["daily"]["interval"], 30.0)
            db.close()

    def test_invalid_stored_job_skipped(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(Path(tmp) / "s.db")
            db.kv_set("jobs", json.dumps([{"name": "bad", "interval": 1.0}]))  # no account
            scheduler = TaskScheduler(database=db)
            self.assertEqual(scheduler.jobs(), [])
            db.close()


if __name__ == "__main__":
    unittest.main()
