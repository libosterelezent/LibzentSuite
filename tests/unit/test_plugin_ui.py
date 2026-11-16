"""UI extension API tests: plugin stat cards + sidebar panels."""
from __future__ import annotations

import asyncio
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "fixtures"))

from core.event_bus import EventBus
from core.plugin_base import PanelSection, StatCard, UIPanel
from core.plugin_manager import PluginManager
from core.settings_manager import SettingsManager


class FakeServices:
    async def send_message(self, ch, content, **kw): pass


class TestUIStats(unittest.IsolatedAsyncioTestCase):
    async def test_cards_collected_per_account(self):
        mgr = PluginManager(
            EventBus(), logging.getLogger("t"),
            SettingsManager(EventBus(), logging.getLogger("t"), global_layer={}),
            plugin_dir="tests/fixtures/plugins",
        )
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        cards = await mgr.ui_stats("alpha")
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["plugin"], "probe")
        self.assertEqual(cards[0]["value"], "ok")
        self.assertEqual(cards[0]["level"], "ok")
        self.assertIn("trigger", cards[1]["label"])

    async def test_no_account_no_cards(self):
        mgr = PluginManager(
            EventBus(), logging.getLogger("t"),
            SettingsManager(EventBus(), logging.getLogger("t"), global_layer={}),
            plugin_dir="tests/fixtures/plugins",
        )
        mgr.discover()
        self.assertEqual(await mgr.ui_stats("ghost"), [])

    async def test_crashing_provider_isolated(self):
        mgr = PluginManager(
            EventBus(), logging.getLogger("t"),
            SettingsManager(EventBus(), logging.getLogger("t"), global_layer={}),
            plugin_dir="tests/fixtures/plugins",
        )
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        # poison the provider itself: a crashing ui_stats contributes
        # nothing (isolation), the error is only logged
        entry = mgr._accounts["alpha"]["probe"]

        async def boom(ctx):
            raise RuntimeError("stats exploded")

        entry.instance.ui_stats = boom
        cards = await mgr.ui_stats("alpha")
        self.assertEqual(cards, [])   # silently skipped

    async def test_disabled_plugin_contributes_nothing(self):
        """The docstring's contract: only ENABLED plugins contribute —
        the gate is the manager's own runtime state."""
        mgr = PluginManager(
            EventBus(), logging.getLogger("t"),
            SettingsManager(EventBus(), logging.getLogger("t"), global_layer={}),
            plugin_dir="tests/fixtures/plugins",
        )
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        await mgr.set_enabled("alpha", "probe", False)
        self.assertEqual(await mgr.ui_stats("alpha"), [])
        self.assertEqual(await mgr.ui_panels(), [])


class TestUIPanels(unittest.IsolatedAsyncioTestCase):
    async def test_panels_collected_and_sorted(self):
        mgr = PluginManager(
            EventBus(), logging.getLogger("t"),
            SettingsManager(EventBus(), logging.getLogger("t"), global_layer={}),
            plugin_dir="tests/fixtures/plugins",
        )
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        panels = await mgr.ui_panels()
        self.assertEqual(len(panels), 1)
        p = panels[0]
        self.assertEqual(p["id"], "probe:overview")
        self.assertEqual(p["plugin"], "probe")
        self.assertEqual(p["title"], "Probe Overview")
        self.assertEqual(len(p["sections"]), 3)
        self.assertEqual(p["sections"][0]["kind"], "metrics")
        self.assertEqual(p["sections"][1]["kind"], "list")
        self.assertEqual(p["sections"][2]["kind"], "text")

    async def test_panel_dedup_by_id(self):
        """Two accounts running the same plugin -> one panel (id-deduped)."""
        mgr = PluginManager(
            EventBus(), logging.getLogger("t"),
            SettingsManager(EventBus(), logging.getLogger("t"), global_layer={}),
            plugin_dir="tests/fixtures/plugins",
        )
        mgr.discover()
        await mgr.attach_account("alpha", allowed={"probe"})
        await mgr.attach_account("beta", allowed={"probe"})
        await mgr.activate_account("alpha", FakeServices())
        await mgr.activate_account("beta", FakeServices())
        panels = await mgr.ui_panels()
        probe_panels = [p for p in panels if p["plugin"] == "probe"]
        self.assertEqual(len(probe_panels), 1)   # deduped

    async def test_no_plugins_no_panels(self):
        mgr = PluginManager(
            EventBus(), logging.getLogger("t"),
            SettingsManager(EventBus(), logging.getLogger("t"), global_layer={}),
            plugin_dir="tests/fixtures/plugins",
        )
        mgr.discover()
        self.assertEqual(await mgr.ui_panels(), [])


class TestDataclasses(unittest.TestCase):
    def test_stat_card_shape(self):
        c = StatCard(label="x", value="1", sub="s", level="ok")
        self.assertEqual((c.label, c.value, c.sub, c.level), ("x", "1", "s", "ok"))

    def test_panel_section_kinds(self):
        for kind in ("metrics", "table", "list", "text"):
            s = PanelSection(kind=kind, title="t", data=None)
            self.assertEqual(s.kind, kind)

    def test_ui_panel_frozen(self):
        p = UIPanel(id="a:b", title="T")
        with self.assertRaises(Exception):
            p.title = "changed"


if __name__ == "__main__":
    unittest.main()
