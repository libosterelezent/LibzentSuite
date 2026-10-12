"""Stage-C unit tests: PluginBase/PluginManager dashboard-UI lifecycle.

Pins the PluginBase integration: ``initialize_dashboard_ui`` /
``attach_dashboard_ui`` are optional and backward-compatible (plugins
without them behave exactly as before), the hook fires during
activation with a DashboardUIToolset, the registration is usable right
after activation, cleaned up after deactivation, deduplicated across
accounts, and an init crash never breaks other plugins or the account.
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from core.dashboard_ui import (
    ActionContext,
    DashboardUIRegistry,
    DashboardUIToolset,
    UIError,
)
from core.event_bus import EventBus
from core.plugin_base import PluginBase
from core.plugin_manager import PluginManager
from core.settings_manager import SettingsManager

LOG = logging.getLogger("t")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"


class FakeServices:
    """Minimal PluginServices stand-in for activation."""

    def __init__(self, account="alpha") -> None:
        self.account = account

    async def send_message(self, channel_id, content, *, priority=1):
        return None

    async def send_dm(self, user_id, content, *, priority=1):
        return None

    @property
    def settings(self):
        return {}


def _plugin_src(body: str) -> str:
    return (
        "from core.plugin_base import PluginBase\n"
        "\n"
        "class P(PluginBase):\n"
        + "\n".join(f"    {line}" for line in body.splitlines())
        + "\n"
    )


_WITH_UI = "\n".join([
    "name = 'withui'",
    "version = '1.0.0'",
    "def __init__(self):",
    "    self.init_calls = []",
    "def initialize_dashboard_ui(self, ui):",
    "    self.init_calls.append(type(ui).__name__)",
    "    button = ui.Button(component_id='btn').set_label('Go')",
    "    button.set_action(lambda ctx, payload: 'clicked')",
    "    page = ui.Page(component_id='page').set_title('With UI')",
    "    page.add_section(ui.Section(component_id='sec')",
    "                     .add_component(button))",
    "    self.attach_dashboard_ui(ui.Layout().add_page(page))",
])

_UI_BOOM = "\n".join([
    "name = 'uiboom'",
    "version = '1.0.0'",
    "def initialize_dashboard_ui(self, ui):",
    "    raise RuntimeError('ui init explodes')",
])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-ui-life-")
        self.addCleanup(self.tmp.cleanup)
        self.plugin_dir = Path(self.tmp.name)
        self.registry = DashboardUIRegistry()

    def _write(self, name: str, body: str) -> None:
        (self.plugin_dir / f"{name}.py").write_text(_plugin_src(body))

    def _manager(self, plugin_dir=None) -> PluginManager:
        bus = EventBus()
        return PluginManager(
            bus, LOG, SettingsManager(bus),
            plugin_dir=str(plugin_dir if plugin_dir is not None
                            else self.plugin_dir),
            ui_registry=self.registry)

    # ------------------------------------------------- backward compatibility
    async def test_plugin_without_hook_works_exactly_as_before(self) -> None:
        # spy + echo define no initialize_dashboard_ui (probe, sharing the
        # fixtures dir, DOES since Stage D — excluded here so this test
        # stays about hook-less plugins)
        manager = self._manager(plugin_dir=FIXTURES)
        await manager.attach_account("alpha", {"spy", "echo"})
        await manager.activate_account("alpha", FakeServices("alpha"))
        spy = manager._accounts["alpha"]["spy"]
        self.assertTrue(spy.active)
        # no UI side effects at all: empty registry, nothing injected
        self.assertEqual(self.registry.list_plugins(), [])
        self.assertFalse(hasattr(spy.instance, "_ui_registry"))

    async def test_base_hooks_are_noops_without_lifecycle(self) -> None:
        plugin = PluginBase.__new__(PluginBase)  # never activated
        plugin.name = "orphan"
        self.assertIsNone(plugin.initialize_dashboard_ui(DashboardUIToolset()))
        with self.assertRaises(UIError):
            plugin.attach_dashboard_ui(DashboardUIToolset().Layout())

    # ------------------------------------------------------------ activation
    async def test_hook_called_during_activation_with_toolset(self) -> None:
        self._write("withui", _WITH_UI)
        manager = self._manager()
        await manager.attach_account("alpha", {"withui"})
        instance = manager._accounts["alpha"]["withui"].instance
        self.assertEqual(instance.init_calls, [])  # not yet
        await manager.activate_account("alpha", FakeServices("alpha"))
        self.assertEqual(instance.init_calls, ["DashboardUIToolset"])
        # the registry reference was injected for attach_dashboard_ui
        self.assertIs(instance._ui_registry, self.registry)
        self.assertTrue(manager._accounts["alpha"]["withui"].active)

    async def test_registration_usable_right_after_activation(self) -> None:
        self._write("withui", _WITH_UI)
        manager = self._manager()
        await manager.attach_account("alpha", {"withui"})
        await manager.activate_account("alpha", FakeServices("alpha"))

        self.assertEqual(manager.ui_registry, self.registry)
        self.assertEqual(self.registry.list_plugins(), ["withui"])
        snapshot = self.registry.get_layout("withui")
        self.assertEqual(snapshot["children"][0]["type"], "page")
        # end-to-end: the action the plugin declared dispatches
        result = await self.registry.dispatch_action(
            "withui", "btn", {"x": 1},
            ActionContext(plugin="withui", component_id="btn",
                          account="alpha", services=FakeServices("alpha")))
        self.assertEqual((result.success, result.message),
                         (True, "clicked"))

    async def test_ui_cleaned_up_after_deactivation(self) -> None:
        self._write("withui", _WITH_UI)
        manager = self._manager()
        await manager.attach_account("alpha", {"withui"})
        await manager.activate_account("alpha", FakeServices("alpha"))
        self.assertEqual(self.registry.list_plugins(), ["withui"])

        await manager.deactivate_account("alpha")
        self.assertEqual(self.registry.list_plugins(), [])
        self.assertIsNone(self.registry.get_layout("withui"))
        # re-activation registers again (fresh lifecycle)
        await manager.activate_account("alpha", FakeServices("alpha"))
        self.assertEqual(self.registry.list_plugins(), ["withui"])

    async def test_multiple_accounts_no_duplicate_registration(self) -> None:
        self._write("withui", _WITH_UI)
        manager = self._manager()
        for account in ("alpha", "beta", "gamma"):
            await manager.attach_account(account, {"withui"})
            await manager.activate_account(account, FakeServices(account))
        calls = [manager._accounts[a]["withui"].instance.init_calls
                 for a in ("alpha", "beta", "gamma")]
        self.assertEqual(calls, [["DashboardUIToolset"], [], []])
        # exactly ONE registration
        self.assertEqual(self.registry.list_plugins(), ["withui"])
        self.assertEqual(len(self.registry._layouts), 1)

    async def test_ui_init_crash_isolated(self) -> None:
        self._write("withui", _WITH_UI)
        self._write("uiboom", _UI_BOOM)
        manager = self._manager()
        await manager.attach_account("alpha", {"withui", "uiboom"})
        await manager.activate_account("alpha", FakeServices("alpha"))

        # the account and BOTH plugins stayed active
        for name in ("withui", "uiboom"):
            runtime = manager._accounts["alpha"][name]
            self.assertTrue(runtime.active, name)
            self.assertTrue(runtime.enabled, name)
        # only the healthy UI registered; the crash left nothing behind
        self.assertEqual(self.registry.list_plugins(), ["withui"])
        # the failure is recorded on the runtime, not swallowed silently
        boom = manager._accounts["alpha"]["uiboom"]
        self.assertEqual(boom.error_count, 1)
        self.assertIn("ui init explodes", boom.last_error)
        # other plugins' dispatch still works after the crash
        ok = await self.registry.dispatch_action(
            "withui", "btn", {},
            ActionContext(plugin="withui", component_id="btn"))
        self.assertEqual(ok.message, "clicked")

    async def test_detached_ui_no_longer_dispatches(self) -> None:
        self._write("withui", _WITH_UI)
        manager = self._manager()
        await manager.attach_account("alpha", {"withui"})
        await manager.activate_account("alpha", FakeServices("alpha"))
        await manager.deactivate_account("alpha")
        with self.assertRaises(UIError):
            await self.registry.dispatch_action(
                "withui", "btn", {},
                ActionContext(plugin="withui", component_id="btn"))


if __name__ == "__main__":
    unittest.main()
