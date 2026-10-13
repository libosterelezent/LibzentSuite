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
