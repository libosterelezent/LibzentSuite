"""Plugin hot-reload and unload tests."""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "fixtures"))

from core.event_bus import EventBus
from core.plugin_manager import PluginManager
from core.settings_manager import SettingsManager


class FakeServices:
    async def send_message(self, ch, content, **kw): pass


def make_manager():
    return PluginManager(
        EventBus(), logging.getLogger("t"),
        SettingsManager(EventBus(), logging.getLogger("t"), global_layer={}),
        plugin_dir="tests/fixtures/plugins",
    )


class TestReloadPlugin(unittest.IsolatedAsyncioTestCase):
    async def test_reload_restores_accounts(self):
        mgr = make_manager()
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        self.assertIn("probe", mgr._accounts["alpha"])

        out = await mgr.reload_plugin("probe")
        self.assertEqual(out["restored_accounts"], 1)
        self.assertIn("probe", mgr._accounts["alpha"])
        # UI re-registered
        self.assertIn("probe", mgr.ui_registry.list_plugins())

    async def test_reload_unknown_plugin_raises(self):
        mgr = make_manager()
        mgr.discover()
        from core.errors import PluginError
        with self.assertRaises(PluginError):
            await mgr.reload_plugin("nope")

    async def test_reload_preserves_disabled_flag(self):
        mgr = make_manager()
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.set_enabled("alpha", "probe", False)
        await mgr.reload_plugin("probe")
        rt = mgr._accounts["alpha"]["probe"]
        self.assertFalse(rt.enabled)

    async def test_reload_with_no_accounts(self):
        mgr = make_manager()
        mgr.discover()
        out = await mgr.reload_plugin("probe")
        self.assertEqual(out["restored_accounts"], 0)

    async def test_reload_reactivates_live_instance(self):
        # H1 regression: reload used to restore instance=None corpses —
        # on an active account the plugin received ZERO events (and the
        # enable toggle forcibly disabled it) until an account restart
        mgr = make_manager()
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        rt = mgr._accounts["alpha"]["probe"]
        self.assertTrue(rt.active)

        out = await mgr.reload_plugin("probe")

        self.assertEqual(out["restored_accounts"], 1)
        self.assertEqual(out["reactivated_accounts"], 1)
        rt = mgr._accounts["alpha"]["probe"]
        self.assertIsNotNone(rt.instance, "restored runtime must be live")
        self.assertTrue(rt.active, "reload must re-activate running plugins")

    async def test_reload_failure_preserves_live_state(self):
        # H3/seam 2.5: a broken re-import must NOT tear down the working
        # copy — validation happens before any detach
        mgr = make_manager()
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        bad = mgr._loader._dir / "probe" / "__init__.py"
        good = bad.read_text()
        bad.write_text("def broken(:\n")
        try:
            from core.errors import PluginError
            with self.assertRaises(PluginError):
                # discover fails; reload must propagate WITHOUT teardown
                await mgr.reload_plugin("__rescan__")
        finally:
            bad.write_text(good)
        rt = mgr._accounts["alpha"]["probe"]
        self.assertTrue(rt.active, "failed rescan must not deactivate")
        self.assertIn("probe", mgr.ui_registry.list_plugins())


class TestUnloadPlugin(unittest.IsolatedAsyncioTestCase):
    async def test_unload_detaches_everywhere(self):
        mgr = make_manager()
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.attach_account("beta", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())

        out = mgr.unload_plugin("probe")
        self.assertEqual(out["detached_accounts"], 2)
        # runtime entries stay (marked inactive) so reload can restore
        rt_a = mgr._accounts.get("alpha", {}).get("probe")
        rt_b = mgr._accounts.get("beta", {}).get("probe")
        self.assertIsNotNone(rt_a)
        self.assertFalse(rt_a.loaded)
        self.assertFalse(rt_a.active)
        self.assertIsNone(rt_a.instance)
        self.assertFalse(rt_b.loaded)
        # UI unregistered
        self.assertNotIn("probe", mgr.ui_registry.list_plugins())

    async def test_unload_unknown_plugin_is_noop(self):
        mgr = make_manager()
        mgr.discover()
        out = mgr.unload_plugin("nope")
        self.assertEqual(out["detached_accounts"], 0)

    async def test_unload_then_reload_restores(self):
        mgr = make_manager()
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())

        mgr.unload_plugin("probe")
        rt = mgr._accounts["alpha"]["probe"]
        self.assertFalse(rt.loaded)

        # reload re-discovers and restores
        out = await mgr.reload_plugin("probe")
        self.assertEqual(out["restored_accounts"], 1)
        self.assertIn("probe", mgr._accounts["alpha"])


if __name__ == "__main__":
    unittest.main()


class TestActivationSettleAndReloadUndo(unittest.IsolatedAsyncioTestCase):
    """Review pass-2 #1 + #2: teardown paths must settle in-flight
    activations and the reload undo must actually deactivate."""

    def _mgr(self):
        return make_manager()

    async def test_deactivate_waits_for_inflight_activation(self):
        import asyncio
        mgr = self._mgr()
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        rt = mgr._accounts["alpha"]["probe"]
        parked = asyncio.Event()
        release = asyncio.Event()

        async def slow_on_enable(*a):
            parked.set()
            await release.wait()

        rt.instance.on_enable = slow_on_enable
        activating = asyncio.create_task(
            mgr.activate_account("alpha", FakeServices()))
        await parked.wait()                     # activation mid-hook
        deactivating = asyncio.create_task(mgr.deactivate_account("alpha"))
        await asyncio.sleep(0.05)
        self.assertFalse(deactivating.done(),
                         "deactivate must WAIT for the activation to settle")
        release.set()
        await activating
        await deactivating
        self.assertFalse(rt.active, "post-settle deactivate takes effect")
        # and the account's services were consumed by the deactivate
        self.assertNotIn("alpha", mgr._services)

    async def test_reload_undo_deactivates_when_services_vanish(self):
        import asyncio
        mgr = self._mgr()
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        rt = mgr._accounts["alpha"]["probe"]
        parked = asyncio.Event()
        release = asyncio.Event()

        async def slow_on_enable(*a):
            parked.set()
            await release.wait()

        rt.instance.on_enable = slow_on_enable
        # park the reload's re-activation inside the hook
        old_create = mgr._loader.create

        def create_parked(name):
            inst = old_create(name)
            if name == "probe":
                inst.on_enable = slow_on_enable
            return inst

        mgr._loader.create = create_parked
        reloading = asyncio.create_task(mgr.reload_plugin("probe"))
        await parked.wait()                     # re-activation mid-hook
        mgr._services.pop("alpha", None)        # the concurrent stop's mark
        release.set()
        out = await reloading
        self.assertEqual(out["reactivated_accounts"], 0)
        new_rt = mgr._accounts["alpha"]["probe"]
        self.assertFalse(new_rt.active,
                         "undo must deactivate the half-activated replacement")
        self.assertFalse(new_rt.loaded)


class TestActivationEpochFence(unittest.IsolatedAsyncioTestCase):
    """Review pass-3 #2: a hook that outlives the settle timeout must not
    resurrect its runtime after teardown finished."""

    async def test_late_activation_is_fenced(self):
        import asyncio
        mgr = make_manager()
        mgr._activation_settle_timeout_s = 0.05   # fence almost at once
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        rt = mgr._accounts["alpha"]["probe"]
        parked = asyncio.Event()
        release = asyncio.Event()

        async def slow_on_enable(*a):
            parked.set()
            await release.wait()

        rt.instance.on_enable = slow_on_enable
        activating = asyncio.create_task(
            mgr.activate_account("alpha", FakeServices()))
        await parked.wait()                     # activation mid-hook
        await mgr.deactivate_account("alpha")   # settle times out → fence
        self.assertEqual(mgr._activation_epoch.get("alpha"), 1)
        release.set()                           # the straggler resumes NOW
        await activating                        # completes, fenced, no raise
        self.assertFalse(rt.active,
                         "fenced activation must not resurrect the runtime")
        self.assertFalse(rt.loaded)

    async def test_fast_activation_is_unaffected(self):
        mgr = make_manager()
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        rt = mgr._accounts["alpha"]["probe"]
        self.assertTrue(rt.active)   # no timeout → no fence → normal path


class TestSummaryRoundThree(unittest.IsolatedAsyncioTestCase):
    """Review-summary round 3: removal-path fence, reload settings."""

    async def test_fenced_straggler_cannot_resurrect_after_detach(self):
        import asyncio
        mgr = make_manager()
        mgr._activation_settle_timeout_s = 0.05
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        rt = mgr._accounts["alpha"]["probe"]
        parked = asyncio.Event()
        release = asyncio.Event()

        async def slow_on_enable(*a):
            parked.set()
            await release.wait()

        rt.instance.on_enable = slow_on_enable
        activating = asyncio.create_task(
            mgr.activate_account("alpha", FakeServices()))
        await parked.wait()
        await mgr.deactivate_account("alpha")   # times out → epoch fence
        mgr.detach_account("alpha")             # removal's detach pops maps
        release.set()
        await activating
        self.assertFalse(rt.active,
                         "fence must survive detach — the epoch marker is "
                         "deliberately retained past account removal")

    async def test_reload_restores_resolved_settings(self):
        mgr = make_manager()
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        rt = mgr._accounts["alpha"]["probe"]
        self.assertTrue(rt.settings,
                        "precondition: activation resolved settings")
        await mgr.deactivate_account("alpha")   # stopped account
        out = await mgr.reload_plugin("probe")
        self.assertEqual(out["restored_accounts"], 1)
        new_rt = mgr._accounts["alpha"]["probe"]
        self.assertTrue(
            new_rt.settings,
            "reload resolves settings at restore (attach contract) — "
            "not only for re-activated runtimes")


class TestPassFourFenceSemantics(unittest.IsolatedAsyncioTestCase):
    """Review pass-4 #3/#4: the fence arms only for real teardown, and a
    fenced activation stops starting the remaining plugins."""

    def _two_runtime_mgr(self):
        mgr = make_manager()
        mgr.discover()
        return mgr

    async def test_fenced_activation_does_not_start_remaining(self):
        import asyncio
        from core.plugin_manager.manager import PluginRuntime
        mgr = self._two_runtime_mgr()
        mgr._activation_settle_timeout_s = 0.05
        await mgr.attach_account("alpha", allowed=set())
        hooked = []

        class FakeInst:
            async def on_load(self, *a): hooked.append("load2")
            async def on_enable(self, *a): hooked.append("enable2")

        # the fake plugin is unknown to the loader — give it an empty
        # schema so _activate_one passes settings resolution (it disables
        # unknown plugins BEFORE running any hook otherwise)
        orig_schema = mgr._schema_for
        mgr._schema_for = \
            lambda n: () if n == "fake2" else orig_schema(n)
        runtimes = mgr._accounts.setdefault("alpha", {})
        runtimes["probe"] = PluginRuntime(name="probe", instance=None,
                                          enabled=False)  # skip via disabled
        runtimes["fake2"] = PluginRuntime(name="fake2", instance=FakeInst(),
                                          enabled=True)

        parked = asyncio.Event()
        release = asyncio.Event()

        async def slow_enable(*a):
            parked.set()
            await release.wait()

        # park INSIDE the first enabled runtime (fake2) by ordering:
        # dict order = probe(disabled, skipped), fake2 first enabled
        runtimes["fake2"].instance.on_enable = slow_enable
        activating = asyncio.create_task(
            mgr.activate_account("alpha", FakeServices()))
        await parked.wait()
        await mgr.deactivate_account("alpha")   # timeout → fence
        release.set()
        await activating
        self.assertEqual(hooked, ["load2"],
                         "no further hooks after the fence (only the "
                         "parked one ran on_load)")

    async def test_reload_timeout_does_not_fence_running_account(self):
        import asyncio
        from core.plugin_manager.manager import PluginRuntime
        mgr = self._two_runtime_mgr()
        mgr._activation_settle_timeout_s = 0.05
        await mgr.attach_account("alpha", allowed=set())
        orig_schema = mgr._schema_for
        mgr._schema_for = \
            lambda n: () if n == "slowplug" else orig_schema(n)
        runtimes = mgr._accounts.setdefault("alpha", {})
        probe_rt = PluginRuntime(name="probe", instance=None, enabled=False)
        runtimes["probe"] = probe_rt

        parked = asyncio.Event()
        release = asyncio.Event()

        class SlowInst:
            async def on_load(self, *a): pass
            async def on_enable(self, *a):
                parked.set()
                await release.wait()

        slow_rt = PluginRuntime(name="slowplug", instance=SlowInst(),
                                enabled=True)
        runtimes["slowplug"] = slow_rt

        activating = asyncio.create_task(
            mgr.activate_account("alpha", FakeServices()))
        await parked.wait()                     # slowplug mid-on_enable
        # reload probe while the activation is unsettled — must NOT fence
        out = await mgr.reload_plugin("probe")
        release.set()
        await activating
        self.assertEqual(out["restored_accounts"], 1)
        self.assertTrue(slow_rt.active,
                        "a reload timing out must not unload plugins on a "
                        "still-running account (fence is teardown-only)")


class TestPassFiveReloadDuringPendingHook(unittest.IsolatedAsyncioTestCase):
    """Review pass-5: reloading the plugin whose activation hook is
    pending must not leave the OLD instance active-but-orphaned."""

    async def test_reload_of_pending_plugin_orphans_nothing(self):
        import asyncio
        mgr = make_manager()
        mgr._activation_settle_timeout_s = 0.05
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        old_rt = mgr._accounts["alpha"]["probe"]
        parked = asyncio.Event()
        release = asyncio.Event()

        async def slow_enable(*a):
            parked.set()
            await release.wait()

        old_rt.instance.on_enable = slow_enable
        activating = asyncio.create_task(
            mgr.activate_account("alpha", FakeServices()))
        await parked.wait()                     # probe mid-on_enable
        out = await mgr.reload_plugin("probe")  # settle times out, no fence
        release.set()                           # the straggler resumes NOW
        await activating
        new_rt = mgr._accounts["alpha"]["probe"]
        self.assertIsNot(new_rt, old_rt, "reload installed the replacement")
        self.assertFalse(
            old_rt.active,
            "the replaced instance must not stay active (orphan with "
            "fired hooks — nothing would ever unload it)")
        self.assertFalse(old_rt.loaded)
        self.assertFalse(new_rt.active,
                         "was_active was False at reload snapshot time")
        self.assertEqual(out["restored_accounts"], 1)


class TestPassSixSingleTeardown(unittest.IsolatedAsyncioTestCase):
    """Review pass-6: on_unload must run EXACTLY once per runtime even
    when reload's unload and the late activation's compensation overlap."""

    async def test_unload_hook_runs_once_when_reload_and_straggler_overlap(self):
        import asyncio
        mgr = make_manager()
        mgr._activation_settle_timeout_s = 0.05
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        rt = mgr._accounts["alpha"]["probe"]
        enable_parked = asyncio.Event()
        enable_release = asyncio.Event()
        unload_parked = asyncio.Event()
        unload_release = asyncio.Event()
        calls = {"on_unload": 0, "on_disable": 0}

        async def slow_enable(*a):
            enable_parked.set()
            await enable_release.wait()

        async def slow_unload(*a):
            calls["on_unload"] += 1
            unload_parked.set()
            await unload_release.wait()

        async def count_disable(*a):
            calls["on_disable"] += 1

        rt.instance.on_enable = slow_enable
        rt.instance.on_unload = slow_unload
        rt.instance.on_disable = count_disable
        activating = asyncio.create_task(
            mgr.activate_account("alpha", FakeServices()))
        await enable_parked.wait()             # activation mid-on_enable
        reloading = asyncio.create_task(mgr.reload_plugin("probe"))
        await unload_parked.wait()             # reload mid-on_unload
        enable_release.set()                   # straggler resumes NOW
        await asyncio.sleep(0.05)              # compensation parks on the
        #                                   same runtime's teardown event
        unload_release.set()                   # reload's unload finishes
        await activating
        out = await reloading
        self.assertEqual(calls["on_unload"], 1,
                         "on_unload must run exactly once despite both "
                         "paths arriving at the same runtime")
        self.assertFalse(rt.active)
        self.assertFalse(rt.loaded)
        self.assertEqual(out["restored_accounts"], 1)


class TestPassSevenTeardownTakeover(unittest.IsolatedAsyncioTestCase):
    """Review pass-7: a CANCELLED teardown must not look completed — the
    waiting teardown takes over and finishes the cleanup."""

    async def test_interrupted_teardown_is_taken_over(self):
        import asyncio
        mgr = make_manager()
        mgr._teardown_wait_timeout_s = 0.05
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        rt = mgr._accounts["alpha"]["probe"]
        self.assertTrue(rt.active)
        disable_parked = asyncio.Event()
        calls = {"on_disable": 0, "on_unload": 0}

        async def slow_disable(*a):
            calls["on_disable"] += 1
            if calls["on_disable"] == 1:
                disable_parked.set()
                await asyncio.sleep(3600)   # parked until cancelled

        async def count_unload(*a):
            calls["on_unload"] += 1

        rt.instance.on_disable = slow_disable
        rt.instance.on_unload = count_unload

        # task A owns the teardown and parks inside on_disable
        owner = asyncio.create_task(mgr.deactivate_account("alpha"))
        await disable_parked.wait()
        # task B arrives while A is mid-hook — it waits for A's outcome
        waiter = asyncio.create_task(mgr._teardown_runtime("alpha", rt))
        await asyncio.sleep(0.02)
        self.assertFalse(waiter.done())
        # A is CANCELLED mid-hook (the bounded stop abandoning deactivate)
        owner.cancel()
        try:
            await owner
        except asyncio.CancelledError:
            pass
        # B times out on the (deliberately unset) event, sees the
        # interruption marker and TAKES OVER the remaining work
        await waiter
        self.assertFalse(rt.active,
                         "the takeover must finish the interrupted cleanup")
        self.assertFalse(rt.loaded)
        self.assertGreaterEqual(calls["on_disable"], 1)
        self.assertEqual(calls["on_unload"], 1)

    async def test_completed_teardown_is_not_redone(self):
        import asyncio
        mgr = make_manager()
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        rt = mgr._accounts["alpha"]["probe"]
        await mgr.deactivate_account("alpha")      # completes normally
        self.assertFalse(rt.active)
        # the done-markers pin the completed hooks
        self.assertTrue(getattr(rt, "_disable_done", False))
        self.assertTrue(getattr(rt, "_unload_done", False))
        calls = []
        rt.instance.on_unload = lambda *a: calls.append(1)
        await mgr._teardown_runtime("alpha", rt)   # second caller skips
        self.assertEqual(calls, [], "completed hooks must not re-run")


class TestPassEightClaimLoop(unittest.IsolatedAsyncioTestCase):
    """Review pass-8: atomic single-owner claims, owed-hook records,
    reactivation re-arming, and load-start marker clears."""

    async def _active_runtime(self, mgr):
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        return mgr._accounts["alpha"]["probe"]

    async def test_retry_after_cancellation_completes_cleanup(self):
        # pass-8 #1: the retry starts only AFTER the cancellation — its
        # was_* snapshot sees the cleared flags, so the owed-hook record
        # must carry what the interrupted attempt still owed
        import asyncio
        mgr = make_manager()
        mgr._teardown_wait_timeout_s = 0.05
        rt = await self._active_runtime(mgr)
        parked = asyncio.Event()
        calls = {"on_disable": 0, "on_unload": 0}

        async def slow_disable(*a):
            calls["on_disable"] += 1
            if calls["on_disable"] == 1:
                parked.set()
                await asyncio.sleep(3600)

        async def count_unload(*a):
            calls["on_unload"] += 1

        rt.instance.on_disable = slow_disable
        rt.instance.on_unload = count_unload
        owner = asyncio.create_task(mgr._teardown_runtime("alpha", rt))
        await parked.wait()
        owner.cancel()                       # bounded stop abandons it
        try:
            await owner
        except asyncio.CancelledError:
            pass
        self.assertFalse(rt.active)          # flags reset by the interrupt
        # the retry arrives with NO waiter alive at cancel time
        await mgr._teardown_runtime("alpha", rt)
        self.assertEqual(calls["on_unload"], 1,
                         "the retry must finish the interrupted cleanup")

    async def test_three_overlapping_callers_single_hook_run(self):
        # pass-8 #2: three callers, the owner cancelled mid-hook —
        # exactly one takeover claims; the others wait for its outcome
        import asyncio
        mgr = make_manager()
        mgr._teardown_wait_timeout_s = 0.05
        rt = await self._active_runtime(mgr)
        parked = asyncio.Event()
        release = asyncio.Event()
        calls = {"on_disable": 0, "on_unload": 0}

        async def slow_disable(*a):
            calls["on_disable"] += 1
            if calls["on_disable"] == 1:
                parked.set()
                await asyncio.sleep(3600)

        async def slow_unload(*a):
            calls["on_unload"] += 1
            if calls["on_unload"] == 1:
                await release.wait()

        rt.instance.on_disable = slow_disable
        rt.instance.on_unload = slow_unload
        owner = asyncio.create_task(mgr._teardown_runtime("alpha", rt))
        await parked.wait()
        w1 = asyncio.create_task(mgr._teardown_runtime("alpha", rt))
        w2 = asyncio.create_task(mgr._teardown_runtime("alpha", rt))
        await asyncio.sleep(0.02)
        owner.cancel()
        try:
            await owner
        except asyncio.CancelledError:
            pass
        done, pending = await asyncio.wait([w1, w2], timeout=3.0)
        if pending:                          # one may still be waiting on
            release.set()                    # the takeover's slow unload
            done, pending = await asyncio.wait([w1, w2], timeout=3.0)
        release.set()
        self.assertFalse(pending)
        self.assertFalse(rt.active)
        self.assertFalse(rt.loaded)
        self.assertEqual(calls["on_unload"], 1,
                         "exactly one unload across owner + takeover")

    async def test_concurrent_deactivation_after_reactivation(self):
        # pass-8 #3: a completed teardown leaves the event set — the
        # next lifecycle's concurrent teardowns must still serialize
        import asyncio
        mgr = make_manager()
        rt = await self._active_runtime(mgr)
        await mgr._teardown_runtime("alpha", rt)   # completes (event set)
        await mgr._activate_one("alpha", rt)       # reactivation re-arms
        self.assertTrue(rt.active)
        calls = []

        async def counted(*a):
            calls.append(1)

        rt.instance.on_unload = counted
        await asyncio.wait([asyncio.create_task(
            mgr._teardown_runtime("alpha", rt)) for _ in range(3)],
            timeout=3.0)
        self.assertEqual(len(calls), 1,
                         "concurrent teardowns after reactivation run "
                         "the unload exactly once")

    async def test_failed_reenable_still_unloads(self):
        # pass-8 #4: on_load succeeds, on_enable FAILS after a prior
        # completed teardown — the later unload must still run for the
        # freshly loaded resources
        mgr = make_manager()
        rt = await self._active_runtime(mgr)
        await mgr.deactivate_account("alpha")       # completed teardown
        self.assertTrue(getattr(rt, "_unload_done", False))
        unloaded = []

        async def fail_enable(*a):
            raise RuntimeError("boom")

        async def count_unload(*a):
            unloaded.append(1)

        rt.instance.on_enable = fail_enable
        rt.instance.on_unload = count_unload
        try:
            await mgr._activate_one("alpha", rt)
        except Exception:
            pass
        self.assertTrue(rt.loaded)                  # the fresh load stuck
        await mgr._teardown_runtime("alpha", rt)
        self.assertEqual(unloaded, [1],
                         "the marker cleared at load start, so the new "
                         "load's unload runs despite the failed enable")
