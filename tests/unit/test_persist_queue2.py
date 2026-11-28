"""Round-2 hostile audit matrix for core._persist_queue (agent 12 of 15).

Stops under sabotage: put-after-stop accounting (airtight vs the flag
flip), stop->start->stop cycles (per-leg order, no cross-leg duplicates),
stop(timeout=0), joiner-vs-owner stop races, _carry preservation across
worker death + restart (ORDER), stragglers racing the inline drain, and
the no-resurrection guarantee: start() must never spawn a second drainer
while a stop() teardown still owns the queue/_carry.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import unittest
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from libzentsuite.core._persist_queue import PersistQueue


class ListRepo:
    """Counting repository with optional per-add delay."""

    def __init__(self, delay: float = 0.0) -> None:
        self.rows: list[int] = []
        self.delay = delay

    async def add(self, entry: dict[str, Any]) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.rows.append(entry["n"])


class GateRepo:
    """Each add() parks until the test releases exactly one step."""

    def __init__(self) -> None:
        self.rows: list[int] = []
        self.arrived = asyncio.Event()
        self.go = asyncio.Event()

    async def add(self, entry: dict[str, Any]) -> None:
        self.arrived.set()
        await self.go.wait()
        self.go.clear()
        self.rows.append(entry["n"])


class BlockRepo:
    """Every add() parks until the gate opens (then never blocks again)."""

    def __init__(self) -> None:
        self.rows: list[int] = []
        self.gate = asyncio.Event()

    async def add(self, entry: dict[str, Any]) -> None:
        await self.gate.wait()
        self.rows.append(entry["n"])


def items(*ns: int) -> list[dict[str, int]]:
    return [{"n": n} for n in ns]


@contextlib.contextmanager
def quiet_stderr() -> Iterator[None]:
    with contextlib.redirect_stderr(io.StringIO()):
        yield


class TestStoppedSemantics(unittest.IsolatedAsyncioTestCase):
    async def test_put_after_stop_counted_dropped(self) -> None:
        pq = PersistQueue("t")
        repo = ListRepo()
        await pq.start(repo, interval=3600.0)
        await asyncio.sleep(0)  # worker parked idle
        pq.put({"n": 1})
        pq.put({"n": 2})
        await pq.stop(5.0)
        self.assertEqual(repo.rows, [1, 2])
        for n in (3, 4, 5):
            pq.put({"n": n})  # must not raise
        self.assertEqual(repo.rows, [1, 2])  # nothing resurrected
        self.assertEqual(pq.stats()["dropped"], 3)
        self.assertEqual(pq._q.qsize(), 0)  # never even enqueued

    async def test_start_after_stop_drains_new_items_in_order(self) -> None:
        pq = PersistQueue("t")
        repo = ListRepo()
        await pq.start(repo, interval=0.01)
        for n in (1, 2, 3):
            pq.put({"n": n})
        await pq.stop(5.0)
        self.assertEqual(repo.rows, [1, 2, 3])
        pq.put({"n": 99})  # between legs: dropped
        self.assertEqual(pq.stats()["dropped"], 1)
        await pq.start(repo, interval=0.01)  # leg 2
        for n in (4, 5, 6):
            pq.put({"n": n})
        await pq.stop(5.0)
        self.assertEqual(repo.rows, [1, 2, 3, 4, 5, 6])  # ordered, no dups
        self.assertEqual(pq.stats()["dropped"], 1)

    async def test_stop_start_stop_three_legs(self) -> None:
        pq = PersistQueue("t")
        repo = ListRepo()
        expected: list[int] = []
        for leg in range(3):
            await pq.start(repo, interval=0.01, batch=2)
            for k in range(3):
                n = leg * 10 + k
                pq.put({"n": n})
                expected.append(n)
            await asyncio.sleep(0.05)  # worker drains within the leg
            await pq.stop(5.0)
            self.assertEqual(repo.rows, expected)
        pq.put({"n": 999})
        self.assertEqual(pq.stats()["dropped"], 1)
        self.assertFalse(pq.active)
        self.assertTrue(pq._stopped)

    async def test_stop_timeout_zero_drops_everything_counted(self) -> None:
        pq = PersistQueue("t")
        repo = ListRepo()
        await pq.start(repo, interval=3600.0)
        await asyncio.sleep(0)  # worker reaches its idle sleep first
        for n in range(5):
            pq.put({"n": n})
        await pq.stop(0.0)
        self.assertEqual(repo.rows, [])  # no budget -> no writes
        self.assertEqual(pq.stats()["dropped"], 5)
        self.assertEqual(pq._q.qsize(), 0)
        self.assertEqual(pq._carry, [])
        pq.put({"n": 6})
        self.assertEqual(pq.stats()["dropped"], 6)


class TestStopRaces(unittest.IsolatedAsyncioTestCase):
    async def test_joiner_stop_joins_owner_single_drain(self) -> None:
        pq = PersistQueue("t")
        repo = ListRepo(delay=0.01)
        await pq.start(repo, interval=0.01, batch=5)
        for n in range(20):
            pq.put({"n": n})

        async def owner() -> None:
            await pq.stop(10.0)

        async def joiner() -> None:
            await pq.stop(10.0)

        with quiet_stderr():
            await asyncio.gather(owner(), joiner())
        self.assertEqual(repo.rows, list(range(20)))  # exactly once, in order
        self.assertEqual(pq.stats()["dropped"], 0)

    async def test_joiner_budget_expiry_warns_loudly_no_raise(self) -> None:
        """Round-3: the joiner's bounded re-join loop must exhaust its OWN
        budget (not bail after one join), then WARN instead of raising —
        raising would abort the caller's remaining shutdown cleanups."""
        pq = PersistQueue("t")
        repo = BlockRepo()
        await pq.start(repo, interval=0.05, batch=10)
        for n in (1, 2):
            pq.put({"n": n})
        await asyncio.sleep(0.05)  # worker parked inside add(item 1)
        owner = asyncio.create_task(pq.stop(30.0))
        await asyncio.sleep(0.2)  # owner's inline drain parked too
        self.assertTrue(pq._stopping)
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            await pq.stop(0.05)  # joiner budget far shorter than drain
        self.assertIn("join timed out", captured.getvalue())
        self.assertIn("do NOT close the database", captured.getvalue())
        self.assertTrue(pq._stopping)  # owner still draining; joiner returned
        repo.gate.set()  # let the owner finish cleanly
        with quiet_stderr():
            await owner
        self.assertEqual(repo.rows, [1, 2])

    async def test_put_during_inline_drain_is_flushed(self) -> None:
        pq = PersistQueue("t")
        repo = ListRepo(delay=0.01)
        await pq.start(repo, interval=0.01, batch=2)
        pq.put({"n": 1})
        pq.put({"n": 2})
        stop_task = asyncio.create_task(pq.stop(10.0))
        await asyncio.sleep(0.02)  # teardown draining batches
        pq.put({"n": 3})  # stragglers inside the stop budget
        pq.put({"n": 4})
        pq.put({"n": 5})
        with quiet_stderr():
            await stop_task
        self.assertEqual(repo.rows, [1, 2, 3, 4, 5])
        self.assertEqual(pq.stats()["dropped"], 0)

    async def test_start_waits_for_inflight_stop_then_drains_new(self) -> None:
        pq = PersistQueue("t")
        repo = BlockRepo()
        await pq.start(repo, interval=0.05, batch=10)
        for n in (1, 2, 3, 4):
            pq.put({"n": n})
        await asyncio.sleep(0.05)  # worker parked inside add(item 1)
        stop_task = asyncio.create_task(pq.stop(30.0))
        await asyncio.sleep(0.7)  # worker cancelled; inline drain parked
        self.assertEqual(repo.rows, [])
        start_task = asyncio.create_task(pq.start(repo, interval=0.05))
        await asyncio.sleep(0.3)
        repo.gate.set()  # release the parked inline batch
        with quiet_stderr():
            await stop_task
        await start_task
        self.assertEqual(repo.rows, [1, 2, 3, 4])
        for n in (5, 6):  # NEW items on the restarted worker
            pq.put({"n": n})
        for _ in range(200):
            if len(repo.rows) == 6:
                break
            await asyncio.sleep(0.005)
        with quiet_stderr():
            await pq.stop(5.0)
        self.assertEqual(repo.rows, [1, 2, 3, 4, 5, 6])
        self.assertEqual(pq.stats()["dropped"], 0)

    async def test_no_second_drainer_while_stop_owns_the_queue(self) -> None:
        """The resurrection race: a start() whose 5s join budget expires
        while stop() still drains must NOT spawn a competing worker (two
        drainers share _carry -> duplicates/disorder)."""
        pq = PersistQueue("t")
        repo = BlockRepo()
        await pq.start(repo, interval=0.05, batch=10)
        for n in (1, 2, 3, 4):
            pq.put({"n": n})
        await asyncio.sleep(0.05)
        stop_task = asyncio.create_task(pq.stop(30.0))
        await asyncio.sleep(0.7)  # inline drain now parked inside add()
        self.assertEqual(repo.rows, [])
        start_task = asyncio.create_task(pq.start(repo, interval=5.0))
        await asyncio.sleep(5.4)  # past the 5s join budget of start()
        self.assertFalse(start_task.done())
        self.assertIsNone(pq._worker)  # no second drainer spawned
        repo.gate.set()  # let the teardown finish
        with quiet_stderr():
            await stop_task
        await start_task
        self.assertEqual(repo.rows, [1, 2, 3, 4])  # once, in order
        self.assertEqual(pq.stats()["dropped"], 0)
        self.assertIsNotNone(pq._worker)
        with quiet_stderr():
            await pq.stop(5.0)


class TestWorkerDeathAndCarry(unittest.IsolatedAsyncioTestCase):
    async def test_carry_preserved_across_worker_death_and_restart(self) -> None:
        """Cancel the worker MID-BATCH: committed rows stay written, the
        unwritten remainder is kept in _carry and written FIRST (order)
        by the restarted worker."""
        pq = PersistQueue("t")
        repo = GateRepo()
        await pq.start(repo, interval=0.05, batch=5)
        for n in (1, 2, 3):
            pq.put({"n": n})
        await asyncio.wait_for(repo.arrived.wait(), 2)  # add(1) parked
        repo.arrived.clear()
        repo.go.set()  # commit row 1
        await asyncio.wait_for(repo.arrived.wait(), 2)  # add(2) parked
        worker = pq._worker
        assert worker is not None
        worker.cancel()  # die mid-batch
        with quiet_stderr():
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(repo.rows, [1])
        self.assertEqual([e["n"] for e in pq._carry], [2, 3])  # kept, ordered
        pq.put({"n": 4})  # queued, ownerless
        pq.put({"n": 5})
        repo2 = ListRepo()  # ungated restart
        await pq.start(repo2, interval=0.01, batch=5)  # restart
        for _ in range(200):
            if len(repo2.rows) == 4:
                break
            await asyncio.sleep(0.005)
        with quiet_stderr():
            await pq.stop(5.0)
        self.assertEqual(repo.rows, [1])
        self.assertEqual(repo2.rows, [2, 3, 4, 5])  # carry first, no dup of 1
        self.assertEqual(pq.stats()["dropped"], 0)

    async def test_worker_death_while_idle_restart_drains_queue(self) -> None:
        pq = PersistQueue("t")
        repo = ListRepo()
        await pq.start(repo, interval=0.01)
        pq.put({"n": 1})
        pq.put({"n": 2})
        for _ in range(200):
            if repo.rows:
                break
            await asyncio.sleep(0.005)
        worker = pq._worker
        assert worker is not None
        worker.cancel()  # idle death, no stop()
        with quiet_stderr():
            await asyncio.gather(worker, return_exceptions=True)
        self.assertFalse(pq.active)
        self.assertFalse(pq._stopped)  # not stopped: accepts puts
        pq.put({"n": 3})
        self.assertEqual(pq.stats()["dropped"], 0)
        await pq.start(repo, interval=0.01)
        for _ in range(200):
            if len(repo.rows) == 3:
                break
            await asyncio.sleep(0.005)
        with quiet_stderr():
            await pq.stop(5.0)
        self.assertEqual(repo.rows, [1, 2, 3])

    async def test_stop_after_worker_died_drains_carry_inline(self) -> None:
        pq = PersistQueue("t")
        repo = GateRepo()
        await pq.start(repo, interval=0.05, batch=5)
        for n in (1, 2):
            pq.put({"n": n})
        await asyncio.wait_for(repo.arrived.wait(), 2)
        worker = pq._worker
        assert worker is not None
        worker.cancel()
        with quiet_stderr():
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual([e["n"] for e in pq._carry], [1, 2])
        stop_task = asyncio.create_task(pq.stop(10.0))
        await asyncio.sleep(0.1)
        repo.arrived.clear()
        repo.go.set()  # inline drain writes 1
        await asyncio.wait_for(repo.arrived.wait(), 2)  # then parks on 2
        repo.go.set()
        with quiet_stderr():
            await stop_task
        self.assertEqual(repo.rows, [1, 2])
        self.assertEqual(pq.stats()["dropped"], 0)


class TestMiscContracts(unittest.IsolatedAsyncioTestCase):
    async def test_stats_shape_and_max_inflight_guard(self) -> None:
        with self.assertRaises(ValueError):
            PersistQueue("t", max_inflight=0)
        pq = PersistQueue("t", max_inflight=1)
        self.assertEqual(pq.stats(), {"db_failures": 0, "dropped": 0})

    async def test_start_none_repo_is_inert(self) -> None:
        pq = PersistQueue("t")
        self.assertIsNone(await pq.start(None))
        pq.put({"n": 1})
        await pq.stop(1.0)  # no worker ever existed: no error
        self.assertEqual(pq.stats()["dropped"], 1)  # swept, not lost


if __name__ == "__main__":
    unittest.main()
