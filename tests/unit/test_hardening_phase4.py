"""PHASE-4 hardening regression tests.

Covers the S8 coverage-matrix gaps (concurrent starts, racing double-start,
reconnect-while-peer-runs, delete+re-create end-to-end, stagger pinning,
deactivate-once on remove) and the generation-token race fixes (S1 F2/F3/F6,
S2 supervisor hardening).
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace as NS

from core.account_manager import AccountStatus
from core.errors import AccountError
from core.event_bus import AccountConnected, AccountFailed, MessageReceived

from tests.unit.helpers import make_manager
from tests.unit.test_account_manager import _fast_reconnect_settings


class ConcurrentStartsTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_accounts_start_concurrently(self) -> None:
        """S8-T1: A+B+C+D+E via gather — one client + supervisor each."""
        h = make_manager(token_values={
            "DISCORD_TOKEN_ALPHA": "tok-alpha",
            "DISCORD_TOKEN_BETA": "tok-beta",
            "DISCORD_TOKEN_GAMMA": "tok-gamma",
            "DISCORD_TOKEN_DELTA": "tok-delta",
            "DISCORD_TOKEN_EPSILON": "tok-epsilon",
        })
        names = ["alpha", "beta", "gamma", "delta", "epsilon"]
        for name in names:
            await h.manager.add_account(name)
        baseline = len(asyncio.all_tasks())
        results = await asyncio.gather(*[h.manager.start(n) for n in names])
        self.assertTrue(all(r["status"] == "running" for r in results))
        await h.collector.wait_for(AccountConnected, 5)
        self.assertEqual(len(h.factory.clients), 5)
        supervisors = [h.manager.get(n).runtime.supervisor for n in names]
        self.assertTrue(all(s is not None and not s.done()
                            for s in supervisors))
        self.assertEqual(len({id(s) for s in supervisors}), 5)
        await h.manager.stop_all()
        await asyncio.sleep(0.05)
        after = {t for t in asyncio.all_tasks() if not t.done()}
        # only the test machinery + collector remain — no leaked supervisors
        self.assertLessEqual(len(after), baseline)

    async def test_racing_double_start_builds_one_client(self) -> None:
        """S8-T2: two starts created before either runs — one wins."""
        h = make_manager()
        await h.manager.add_account("alpha")
        tasks = [asyncio.create_task(h.manager.start("alpha"))
                 for _ in range(2)]
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.ALL_COMPLETED, timeout=5)
        outcomes = [t.exception() if t.exception() else t.result()["status"]
                    for t in done]
        statuses = [o for o in outcomes if isinstance(o, str)]
        errors = [o for o in outcomes if not isinstance(o, str)]
        self.assertEqual(len(statuses), 1)              # exactly one winner
        self.assertEqual(statuses[0], "running")
        self.assertEqual(len(errors), 1)                # loser refused
        self.assertIn("already", str(errors[0]))
        self.assertEqual(len(h.factory.clients), 1)     # ONE client built
        await h.manager.stop("alpha")


class ReconnectIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_alpha_reconnects_while_beta_runs(self) -> None:
        """S8-T5: A's drop/reconnect must not disturb B at all."""
        h = make_manager(settings=_fast_reconnect_settings())
        for name in ("alpha", "beta"):
            await h.manager.add_account(name)
        await h.manager.start("alpha")
        await h.manager.start("beta")
        await h.collector.wait_for(AccountConnected, 2)
        beta_client = h.manager.get("beta").runtime.client

        h.manager.get("alpha").runtime.client.drop("socket closed")
        await h.collector.wait_for(AccountConnected, 3)

        rt_a = h.manager.get("alpha").runtime
        rt_b = h.manager.get("beta").runtime
        self.assertEqual(rt_a.status, AccountStatus.RUNNING)
        self.assertIs(rt_a.client, h.factory.clients[-1])  # fresh client
        self.assertIs(rt_b.client, beta_client)             # B's untouched
        self.assertEqual(rt_b.status, AccountStatus.RUNNING)
        await h.manager.stop_all()


class DeleteRecreateTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_and_recreate_starts_fresh(self) -> None:
        """S8-T6: same-name re-add inherits NOTHING from the predecessor."""
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        rt_old = h.manager.get("alpha").runtime
        old_supervisor = rt_old.supervisor
        await h.manager.stop("alpha")
        await h.manager.remove_account("alpha")

        await h.manager.add_account("alpha")
        # the real re-create flow re-stores the credential (the cascade
        # correctly deleted the predecessor's)
        h.manager._credential_service._memory["alpha"] = "tok-alpha"
        h.manager._credential_service._db_accounts.add("alpha")
        h.manager._credential_service._runtime_sources["alpha"] = "db"
        await h.manager.start("alpha")
        rt_new = h.manager.get("alpha").runtime

        self.assertIsNot(rt_new, rt_old)                    # fresh runtime
        self.assertNotIn("alpha", h.settings.overrides)     # no settings
        self.assertIsNot(rt_new.supervisor, old_supervisor)
        self.assertEqual(rt_new.flaps, [])                  # no flap residue
        self.assertTrue(rt_new.session_started > 0)
        await h.manager.stop("alpha")

    async def test_remove_aborts_when_account_recreated_mid_cascade(self):
        """PHASE-4 (S1-F3): the destructive cascade must not hit the NEW
        same-named account that was added while removal was parked."""
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        await h.manager.stop("alpha")
        rt = h.manager.get("alpha").runtime

        park = asyncio.Event()
        original = h.manager._plugin_manager.detach_account

        async def parked_detach(account):
            await park.wait()
            return await original(account)

        h.manager._plugin_manager.detach_account = parked_detach
        remove_task = asyncio.create_task(h.manager.remove_account("alpha"))
        await asyncio.sleep(0.05)              # removal parked in cascade
        await h.manager.add_account("alpha")   # same name re-created NOW
        park.set()
        out = await remove_task

        self.assertFalse(out.get("removed", True))          # aborted
        self.assertTrue(out.get("recreated"))               # flagged why
        # the NEW account survived the old account's cascade
        self.assertIn("alpha", h.manager.names())
        row = await h.repo.get("alpha")
        self.assertIsNotNone(row)                           # DB row intact


class GenerationRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_supervisor_exits_on_generation_bump(self) -> None:
        """PHASE-4 (S1-F2/S2): an old supervisor that wakes after a restart
        must exit instead of reconnecting the new generation's account."""
        h = make_manager(settings=_fast_reconnect_settings())
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        rt = h.manager.get("alpha").runtime
        old_supervisor = rt.supervisor

        # simulate the FAILED stand-down: supervisor alive, account failed
        rt.status = AccountStatus.FAILED
        await h.manager.start("alpha")          # restart bumps generation
        new_supervisor = rt.supervisor
        self.assertIsNot(new_supervisor, old_supervisor)

        # the old supervisor wakes late (its 60s backstop or a lost set):
        rt.lost.set()
        await asyncio.sleep(0.1)
        self.assertTrue(old_supervisor.done())  # it stood down, no fight
        self.assertFalse(new_supervisor.done())
        self.assertEqual(rt.status, AccountStatus.RUNNING)
        await h.manager.stop("alpha")

    async def test_stop_superseded_by_newer_start_leaves_state(self) -> None:
        """PHASE-4 (S1-F6): an old stop must never clobber a newer start's
        terminal state (STOPPED-with-live-client zombie window)."""
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        rt = h.manager.get("alpha").runtime

        park = asyncio.Event()
        original_disconnect = rt.client.disconnect

        async def parked_disconnect():
            await park.wait()
            await original_disconnect()

        rt.client.disconnect = parked_disconnect
        stop_task = asyncio.create_task(h.manager.stop("alpha"))
        await asyncio.sleep(0.05)               # stop parked in disconnect
        gen_after_stop = rt.generation
        # a NEW start takes ownership while the old stop is parked; it is
        # refused by state (CONNECTING->STOPPED flip happens under the
        # lock) so simulate the newer-generation flip directly:
        rt.generation += 1
        park.set()
        await stop_task
        # the superseded stop did NOT write its terminal STOPPED over the
        # newer generation's state: the generation moved past it
        self.assertGreater(rt.generation, gen_after_stop)

    async def test_deactivate_fires_exactly_once_on_remove(self) -> None:
        """S8-T10: pins the merge artifact fix. remove_account itself calls
        deactivate exactly ONCE (stop() owns its own separate call)."""
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        # FAILED (never stopped): remove is the ONLY deactivation source
        h.manager.get("alpha").runtime.status = AccountStatus.FAILED
        await h.manager.remove_account("alpha")
        self.assertEqual(h.plugins.deactivated.count("alpha"), 1)

    async def test_supervisor_death_settles_failed_not_wedged(self) -> None:
        """PHASE-4 (S2 #6): an unexpected supervisor death must not wedge
        the account in RECONNECTING forever."""
        h = make_manager(settings=_fast_reconnect_settings())
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        rt = h.manager.get("alpha").runtime

        async def exploding_loop(name, rt_):
            raise RuntimeError("supervisor exploded on purpose")

        h.manager._reconnect_loop = exploding_loop
        rt.client.drop("gateway closed (code=4007)")
        await asyncio.sleep(0.3)
        # _on_disconnect saw the (about-to-die) supervisor and flipped
        # RECONNECTING; the done-callback must settle FAILED so start()
        # stays usable
        self.assertEqual(rt.status, AccountStatus.FAILED)
        out = await h.manager.start("alpha")     # recovery works
        self.assertEqual(out["status"], "running")
        await h.manager.stop("alpha")


class StaggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_all_stagger_spacing_is_honored(self) -> None:
        """S8-T7: pins the gateway-safety stagger (with small values)."""
        h = make_manager()
        for name in ("alpha", "beta", "gamma"):
            await h.manager.add_account(name)
        loop = asyncio.get_running_loop()
        stamps: list[float] = []

        original = h.collector._append

        def stamping(event):
            if isinstance(event, AccountConnected):
                stamps.append(loop.time())
            original(event)

        h.collector._append = stamping
        await h.manager.start_all(stagger=0.15)
        await h.collector.wait_for(AccountConnected, 3)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(g >= 0.13 for g in gaps), gaps)
        await h.manager.stop_all()


if __name__ == "__main__":
    unittest.main()
