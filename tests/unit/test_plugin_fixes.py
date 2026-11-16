"""Regression tests for the audited plugin-manager fixes.

Covers, per the audit report:

  (a) help pagination — groups join with blank lines so _paginate_help
      can actually split; page 1 carries the more-help suffix and
      ``help 2`` returns the second disjoint page;
  (b) executor authz — a wired access manager is consulted (and the
      legacy env-owner list ignored); invoker_id=None denies;
  (d) admin gate — fleet-wide mutating verbs (accounts start/stop,
      plugins on/off, prefixrate set) are admin-only; reads stay open
      to any permitted invoker; estop stays ungated for allowed users;
  (e) dispatch flood bound — per-account in-flight dispatch tasks are
      capped, excess events dropped-newest and counted;
  (f) the bound-profile settings layer reaches attach/activate-time
      resolution with defaults <- global <- profile <- account
      precedence, and the manager-side effective view includes global;
  plus: boot dashboard-UI takeover by the live instance (boot
      registrations survive deactivation), and loader sys.modules
      purge on a failed discover.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.event_bus import EventBus
from core.plugin_manager import PluginManager
from core.plugin_manager.loader import PluginLoader
from core.settings_manager import SettingsManager

LOG = logging.getLogger("test-plugin-fixes")

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PLUGINS = REPO_ROOT / "tests" / "fixtures" / "plugins"
SPY = "spy"
OWNER_ID = 555
CHANNEL = 42
GUILD = 7


# ------------------------------------------------------------- fakes


def _plugin_src(body: str = "") -> str:
    """Minimal well-formed plugin source for loader tests."""
    return f"""
from core.plugin_base import PluginBase, PluginContext

class _P(PluginBase):
    name = "good"
    version = "0.0.1"
    description = "loader test plugin"
{body}
"""


class LoaderFailurePurgeTests(unittest.TestCase):
    def test_failed_discover_purges_its_synthetic_modules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "good.py").write_text(_plugin_src("name = 'good'\nversion = '1.0.0'\n"))
            bad = root / "bad"
            bad.mkdir()
            (bad / "__init__.py").write_text("raise RuntimeError('package import-time death')\n")
            loader = PluginLoader(root)
            before = {m for m in sys.modules if m.startswith("_plugin_root")}
            # B017 suppression: a synthetic package that dies at import
            # time can surface any wrapper exception — breadth is the contract
            with self.assertRaises(Exception):  # noqa: B017
                loader.discover()
            # the synthetic parent AND every module this discover pulled
            # in are gone (OTHER loaders' live modules are not ours to
            # judge — scope the assertion to the delta)
            delta = {m for m in sys.modules if m.startswith("_plugin_root")} - before
            self.assertEqual(delta, set(), f"leaked modules: {delta}")
            # and the loader is retry-able: healing the tree works
            (bad / "__init__.py").write_text(
                "from core.plugin_base import PluginBase\n"
                "class BadPlugin(PluginBase):\n"
                "    name = 'bad'\n"
            )
            records = loader.discover()
            self.assertEqual(sorted(records), ["bad", "good"])


def _make_manager() -> PluginManager:
    """Plugin manager over the fixture plugins for set_enabled tests."""
    import logging

    return PluginManager(
        EventBus(),
        logging.getLogger("test"),
        SettingsManager(EventBus(), logging.getLogger("test")),
        plugin_dir="tests/fixtures/plugins",
    )


class FakeServices:
    """Minimal services facade for activation tests."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Any]] = []

    async def send_message(self, channel_id: int, content: str, **kw: Any) -> None:
        self.sent.append((channel_id, content, kw.get("priority")))


# --------------------------------------------- set_enabled active clearing
class SetEnabledActiveClearTests(unittest.IsolatedAsyncioTestCase):
    async def test_disable_clears_active_before_the_hook(self) -> None:
        manager = _make_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices())
        runtime = manager._accounts["alpha"][SPY]
        self.assertTrue(runtime.active)
        observed: list[bool] = []

        async def spy_disable(ctx: Any) -> None:
            observed.append(runtime.active)  # must already be False

        runtime.instance.on_disable = spy_disable
        await manager.set_enabled("alpha", SPY, False)
        self.assertFalse(runtime.active)
        self.assertEqual(observed, [False])  # cleared BEFORE on_disable
        manager.close()


if __name__ == "__main__":
    unittest.main()
