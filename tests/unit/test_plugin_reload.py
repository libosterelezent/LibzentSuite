"""Plugin hot-reload and unload tests."""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

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

        out = mgr.reload_plugin("probe")
        self.assertEqual(out["restored_accounts"], 1)
        self.assertIn("probe", mgr._accounts["alpha"])
        # UI re-registered
        self.assertIn("probe", mgr.ui_registry.list_plugins())

    async def test_reload_unknown_plugin_raises(self):
        mgr = make_manager()
        mgr.discover()
        from core.errors import PluginError
        with self.assertRaises(PluginError):
            mgr.reload_plugin("nope")

    async def test_reload_preserves_disabled_flag(self):
        mgr = make_manager()
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.set_enabled("alpha", "probe", False)
        mgr.reload_plugin("probe")
        rt = mgr._accounts["alpha"]["probe"]
        self.assertFalse(rt.enabled)

    async def test_reload_with_no_accounts(self):
        mgr = make_manager()
        mgr.discover()
        out = mgr.reload_plugin("probe")
        self.assertEqual(out["restored_accounts"], 0)


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
        out = mgr.reload_plugin("probe")
        self.assertEqual(out["restored_accounts"], 1)
        self.assertIn("probe", mgr._accounts["alpha"])


if __name__ == "__main__":
    unittest.main()
