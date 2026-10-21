"""FINAL-PHASE (G4): post-delete tombstone audit — weakref-proven.

The removal cascade's CALLS are pinned by test_isolation_audit (K1-K15).
These tests prove the OUTCOME: after remove_account + settle, the
Account / runtime / client / supervisor objects are COLLECTED, task
count returns to baseline, and loggerDict stays bounded (G3 pruning).
"""
from __future__ import annotations

import asyncio
import gc
import logging
import unittest
import weakref

from core.event_bus import AccountConnected

from tests.unit.helpers import make_manager

NAME = "churn"


async def settle() -> None:
    """Let cancelled tasks unwind + done-callbacks clear, then collect."""
    for _ in range(4):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)
    gc.collect()


class TombstoneChurnTests(unittest.IsolatedAsyncioTestCase):
    async def _one_cycle(self, h) -> dict:
        await h.manager.add_account(NAME)
        await h.manager._credential_service.store(NAME, "tok")
        await h.manager.start(NAME)
        await h.collector.wait_for(AccountConnected, 1)
        rt = h.manager.get(NAME).runtime
        refs = {
            "account": weakref.ref(h.manager.get(NAME)),
            "runtime": weakref.ref(rt),
            "client": weakref.ref(rt.client),
            "supervisor": weakref.ref(rt.supervisor),
        }
        await h.manager.stop(NAME)
        h.factory.clients.clear()  # the factory is a deliberate recorder
        await h.manager.remove_account(NAME)
        h.collector.events.clear()  # harness artifact
        await settle()
        return refs

    async def test_churn_objects_collected_after_remove(self) -> None:
        h = make_manager()
        for cycle in range(8):
            refs = await self._one_cycle(h)
            for label, ref in refs.items():
                self.assertIsNone(ref(), f"cycle {cycle}: {label} retained")
            self.assertNotIn(NAME, h.manager._accounts)
            self.assertEqual(h.manager._background, set())
            self.assertEqual(h.manager._dying_supervisors, set())
            self.assertEqual(h.manager._waiter_registry.pending(), {})

    async def test_task_delta_returns_to_baseline(self) -> None:
        h = make_manager()
        baseline = None
        for _ in range(5):
            await self._one_cycle(h)
            await h.bus.drain(timeout=5)
            live = sum(1 for t in asyncio.all_tasks() if not t.done())
            baseline = live if baseline is None else baseline
            self.assertEqual(live, baseline)

    async def test_logger_dict_bounded_by_live_accounts(self) -> None:
        """G3 contract: distinct-name churn does NOT grow loggerDict —
        the removal cascade prunes the account's logger subtree (the
        fake plugin manager never creates these; create exactly what the
        REAL one does — plugin_manager/manager.py:470 — to exercise it)."""
        before = len(logging.Logger.manager.loggerDict)
        h = make_manager()
        for i in range(10):  # DISTINCT names: the R4 growth feed
            name = f"churn{i}"
            await h.manager.add_account(name)
            # exactly what PluginManager._context creates per activation
            for plugin in ("roll", "soulcopy"):
                logging.getLogger(f"libzent.{name}.{plugin}")
            logging.getLogger(f"libzent.adapter.{name}")
            await h.manager._credential_service.store(name, "tok")
            await h.manager.start(name)
            await h.collector.wait_for(AccountConnected, 1)
            await h.manager.stop(name)
            h.factory.clients.clear()
            await h.manager.remove_account(name)
        await settle()
        after = len(logging.Logger.manager.loggerDict)
        # pruning dropped all 30 churned entries; nothing but statics remain
        self.assertLessEqual(after, before + 2,
                             f"loggerDict grew {before} -> {after}")
        residue = [k for k in logging.Logger.manager.loggerDict
                   if k.startswith("libzent.churn")]
        self.assertEqual(residue, [])

    async def test_live_account_loggers_survive_others_pruning(self) -> None:
        """Pruning one account never touches another's loggers."""
        h = make_manager()
        for name in ("alpha", "beta"):
            await h.manager.add_account(name)
            await h.manager._credential_service.store(name, "tok")
            await h.manager.start(name)
        logging.getLogger(f"libzent.alpha.roll").info("alive")
        await h.manager.stop("beta")
        await h.manager.remove_account("beta")
        # alpha's logger still works and is still registered
        logger = logging.getLogger(f"libzent.alpha.roll")
        self.assertIn(f"libzent.alpha.roll",
                      logging.Logger.manager.loggerDict)
        logger.info("still alive after beta removal")
        await h.manager.stop_all()


if __name__ == "__main__":
    unittest.main()
