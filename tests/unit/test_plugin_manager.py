"""PluginManager tests: discovery, attach/activate, toggles, dispatch isolation."""
from __future__ import annotations

import asyncio
import logging
import unittest
from pathlib import Path

from core.event_bus import EventBus, MessageReceived
from core.errors import PluginError, SettingsValidationError
from core.plugin_manager import PluginLoader, PluginManager
from core.settings_manager import SettingsManager

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"
BAD_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "plugins_bad"


def spy_module(manager: PluginManager):
    """Live module object of the loaded spy fixture.

    The loader imports plugins under synthetic module names, so tests MUST
    read fixture state through this accessor (importing the fixture file
    directly would create a second, disconnected copy — the documented trap).
    """
    import sys

    cls = manager._loader.record("spy").cls
    return sys.modules[cls.__module__]


class FakeRepo:
    """Dict-backed stand-in for database PluginRepository."""

    def __init__(self) -> None:
        self.states: dict = {}
        self.settings_rows: dict = {}
        self.metadata: dict = {}

    async def state(self, account, plugin):
        return self.states.get((account, plugin))

    async def set_state(self, account, plugin, enabled):
        self.states[(account, plugin)] = bool(enabled)

    async def set_setting(self, account, plugin, key, value):
        self.settings_rows[(account, plugin, key)] = value

    async def all_account_settings(self):
        out: dict = {}
        for (acct, plug, key), value in self.settings_rows.items():
            out.setdefault(acct, {}).setdefault(plug, {})[key] = value
        return out

    async def upsert_metadata(self, name, version, description, settings_manifest):
        self.metadata[name] = {
            "name": name, "version": version, "description": description,
            "settings": settings_manifest,
        }


class FakeServices:
    """Minimal PluginServices stand-in (records sends)."""

    def __init__(self, account="alpha", plugin="spy") -> None:
        self.account = account
        self.plugin = plugin
        self.sent: list = []

    async def send_message(self, channel_id, content, *, priority=1):
        self.sent.append((channel_id, content, int(priority)))
        return None

    async def send_dm(self, user_id, content, *, priority=1):
        self.sent.append((f"dm:{user_id}", content, int(priority)))
        return None

    async def open_dm(self, user_id):
        return 1_000_000 + user_id

    async def wait_for_message(self, check, timeout):
        return None

    async def collect_messages(self, check, idle_timeout):
        return []

    def publish(self, event):
        pass

    def validate_channel(self, channel_id):
        pass

    def describe_channel(self, channel_id):
        return f"channel-{channel_id}"

    @property
    def credentials(self):
        raise NotImplementedError

    @property
    def settings(self):
        return {}


def make_manager(repo=None, global_layer=None) -> tuple:
    bus = EventBus()
    settings = SettingsManager(bus, logging.getLogger("t"), global_layer=global_layer)
    manager = PluginManager(
        bus, logging.getLogger("t"), settings, plugin_repo=repo,
        plugin_dir=str(FIXTURES),
    )
    return manager, bus, settings


class TestLoader(unittest.TestCase):
    def test_discovers_package_and_single_file(self) -> None:
        loader = PluginLoader(FIXTURES)
        records = loader.discover()
        self.assertIn("spy", records)
        self.assertIn("echo", records)

    def test_bad_name_rejected(self) -> None:
        loader = PluginLoader(BAD_DIR)
        with self.assertRaises(PluginError):
            loader.discover()

    def test_manifests_shape(self) -> None:
        loader = PluginLoader(FIXTURES)
        loader.discover()
        manifests = loader.manifests()
        spy = next(m for m in manifests if m["name"] == "spy")
        self.assertEqual(spy["version"], "1.0.0")
        self.assertTrue(any(s["name"] == "count" for s in spy["settings"]))


class TestLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        import sys
        self.spy_module = None

    async def test_attach_activate_deactivate_order(self) -> None:
        manager, bus, _ = make_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices("alpha"))
        # spy + echo both attached; only spy records hooks
        spy = manager._accounts["alpha"]["spy"]
        self.assertTrue(spy.active)
        # settings resolved incl. defaults
        self.assertEqual(spy.settings["greeting"], "hi")
        self.assertEqual(spy.settings["count"], 3)
        await manager.deactivate_account("alpha")
        self.assertFalse(spy.active)

    async def test_attach_allowlist_filters(self) -> None:
        manager, _, _ = make_manager()
        await manager.attach_account("alpha", allowed={"spy"})
        self.assertEqual(manager.allowed_names("alpha"), ["spy"])

    async def test_enabled_state_from_repo(self) -> None:
        repo = FakeRepo()
        repo.states[("alpha", "spy")] = False
        manager, _, _ = make_manager(repo=repo)
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices("alpha"))
        spy = manager._accounts["alpha"]["spy"]
        self.assertFalse(spy.enabled)
        self.assertFalse(spy.active)  # never activated

    async def test_set_enabled_persists_first_and_fires_hooks(self) -> None:
        repo = FakeRepo()
        manager, _, _ = make_manager(repo=repo)
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices("alpha"))
        spy = manager._accounts["alpha"]["spy"]
        self.assertTrue(spy.active)

        await manager.set_enabled("alpha", "spy", False)
        self.assertFalse(spy.enabled)
        # the on_disable hook fired on the instance
        inst = manager._accounts["alpha"]["spy"].instance
        self.assertIn("on_disable", inst.calls)
        state = {s["name"]: s for s in manager.states("alpha")}["spy"]
        self.assertFalse(state["enabled"])
        self.assertTrue(repo.states[("alpha", "spy")] is False)
        await manager.set_enabled("alpha", "spy", True)
        self.assertTrue({s["name"]: s for s in manager.states("alpha")}["spy"]["enabled"])
        self.assertEqual(inst.calls.count("on_enable"), 2)  # re-enable fires it again

    async def test_set_enabled_unknown_plugin_raises(self) -> None:
        manager, _, _ = make_manager()
        await manager.attach_account("alpha")
        with self.assertRaises(PluginError):
            await manager.set_enabled("alpha", "nope", True)

    async def test_set_setting_validates_and_refreshes(self) -> None:
        manager, _, _ = make_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices("alpha"))
        out = await manager.set_setting("alpha", "spy", "count", 7)
        self.assertEqual(out, 7)
        spy = manager._accounts["alpha"]["spy"]
        self.assertEqual(spy.settings["count"], 7)  # live view refreshed
        with self.assertRaises(SettingsValidationError):
            await manager.set_setting("alpha", "spy", "count", 99)  # > max 10
        with self.assertRaises(SettingsValidationError):
            await manager.set_setting("alpha", "spy", "unknown_key", 1)


class TestDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_routing_and_isolation(self) -> None:
        manager, bus, _ = make_manager()
        await manager.attach_account("alpha")
        await manager.attach_account("beta")
        await manager.activate_account("alpha", FakeServices("alpha"))
        await manager.activate_account("beta", FakeServices("beta"))

        # an alpha message routes to alpha's spy only
        spy_a = manager._accounts["alpha"]["spy"].instance
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=2, channel_id=5, author_id=9, content="again",
        ))
        await asyncio.sleep(0.05)
        self.assertEqual(len(spy_a.seen), 1)
        self.assertEqual(spy_a.seen[0].content, "again")

        # a crashing handler is contained: error recorded, dispatch continues
        runtime_a = manager._accounts["alpha"]["spy"]
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=3, channel_id=5, author_id=9, content="boom-spy",
        ))
        await asyncio.sleep(0.05)
        self.assertEqual(runtime_a.error_count, 1)
        self.assertIn("RuntimeError", runtime_a.last_error)

        # beta message does NOT reach alpha's spy (separate instance)
        seen_before = len(spy_a.seen)
        bus.publish_nowait(MessageReceived(
            account="beta", message_id=4, channel_id=5, author_id=9, content="beta-msg",
        ))
        await asyncio.sleep(0.05)
        self.assertEqual(len(spy_a.seen), seen_before)

    async def test_disabled_plugin_not_dispatched(self) -> None:
        manager, bus, _ = make_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices("alpha"))
        await manager.set_enabled("alpha", "spy", False)
        spy = manager._accounts["alpha"]["spy"].instance
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=5, channel_id=5, author_id=9, content="x",
        ))
        await asyncio.sleep(0.05)
        self.assertEqual(spy.seen, [])

    async def test_services_factory_form(self) -> None:
        manager, _, _ = make_manager()
        await manager.attach_account("alpha")
        made: dict = {}

        def factory(plugin_name: str) -> FakeServices:
            made[plugin_name] = FakeServices("alpha", plugin_name)
            return made[plugin_name]

        await manager.activate_account("alpha", factory)
        self.assertIn("spy", made)
        self.assertIn("echo", made)
        # a plain object is shared across plugins
        shared = FakeServices("alpha")
        await manager.deactivate_account("alpha")
        await manager.activate_account("alpha", shared)
        self.assertTrue(manager._accounts["alpha"]["spy"].active)


class TestMetadataPersist(unittest.IsolatedAsyncioTestCase):
    """discover() is repo-write-free now; persist_metadata() (boot path)
    upserts the manifest rows."""

    async def test_discover_alone_writes_nothing(self) -> None:
        repo = FakeRepo()
        manager, _, _ = make_manager(repo=repo)
        manager.discover()
        self.assertEqual(repo.metadata, {})

    async def test_persist_metadata_upserts_rows(self) -> None:
        repo = FakeRepo()
        manager, _, _ = make_manager(repo=repo)
        manager.discover()
        written = await manager.persist_metadata()
        self.assertGreaterEqual(written, 2)  # spy + echo
        self.assertIn("spy", repo.metadata)
        self.assertEqual(repo.metadata["spy"]["version"], "1.0.0")
        self.assertTrue(any(s["name"] == "count" for s in repo.metadata["spy"]["settings"]))

    async def test_persist_metadata_without_repo_is_a_noop(self) -> None:
        manager, _, _ = make_manager()
        manager.discover()
        self.assertEqual(await manager.persist_metadata(), 0)


if __name__ == "__main__":
    unittest.main()
