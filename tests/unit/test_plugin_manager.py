"""PluginManager tests: discovery, attach/activate, toggles, dispatch isolation."""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path

from core.event_bus import EventBus, MessageReceived, PluginEnabledChanged
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


def make_manager(repo=None, global_layer=None, plugin_dir=None,
                 dispatch_grace_s=1.0) -> tuple:
    bus = EventBus()
    settings = SettingsManager(bus, logging.getLogger("t"), global_layer=global_layer)
    manager = PluginManager(
        bus, logging.getLogger("t"), settings, plugin_repo=repo,
        plugin_dir=str(plugin_dir if plugin_dir is not None else FIXTURES),
        dispatch_grace_s=dispatch_grace_s,
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

    async def test_set_enabled_publishes_final_state(self) -> None:
        """When on_enable crashes, the runtime ends disabled — the event
        must report that final state, not the requested one."""
        manager, bus, _ = make_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices("alpha"))
        events: list = []
        bus.subscribe(PluginEnabledChanged, events.append)
        spy = manager._accounts["alpha"]["spy"]

        async def _boom(ctx):
            raise RuntimeError("enable explodes")

        spy.instance.on_enable = _boom
        await manager.set_enabled("alpha", "spy", False)
        await manager.set_enabled("alpha", "spy", True)
        await asyncio.sleep(0.05)  # publish_nowait dispatch tick
        self.assertFalse(spy.enabled)  # activation failed -> disabled
        self.assertEqual(
            [(e.plugin, e.enabled) for e in events],
            [("spy", False), ("spy", False)],
        )


class TestConstructorIsolation(unittest.IsolatedAsyncioTestCase):
    """A plugin whose __init__ explodes must not break the account."""

    async def test_broken_constructor_skips_plugin_not_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            boom = Path(tmp) / "boom"
            boom.mkdir()
            (boom / "__init__.py").write_text(
                "from core.plugin_base import PluginBase\n"
                "class BoomPlugin(PluginBase):\n"
                "    name = 'boom'\n"
                "    def __init__(self):\n"
                "        raise RuntimeError('constructor explodes')\n"
            )
            ok = Path(tmp) / "ok"
            ok.mkdir()
            (ok / "__init__.py").write_text(
                "from core.plugin_base import PluginBase\n"
                "class OkPlugin(PluginBase):\n"
                "    name = 'ok'\n"
                "    def __init__(self):\n"
                "        self.seen = []\n"
                "    async def handle_event(self, ctx, event):\n"
                "        self.seen.append(event)\n"
            )
            manager, bus, _ = make_manager(plugin_dir=Path(tmp))
            await manager.attach_account("alpha")
            self.assertEqual(manager.allowed_names("alpha"), ["boom", "ok"])
            await manager.activate_account("alpha", FakeServices("alpha"))
            self.assertTrue(manager._accounts["alpha"]["ok"].active)
            boom_rt = manager._accounts["alpha"]["boom"]
            self.assertFalse(boom_rt.enabled)
            self.assertFalse(boom_rt.active)
            self.assertIn("constructor explodes", boom_rt.last_error)
            state = {s["name"]: s for s in manager.states("alpha")}["boom"]
            self.assertEqual(state["error_count"], 1)
            # dispatch to the healthy plugin still works
            ok_inst = manager._accounts["alpha"]["ok"].instance
            bus.publish_nowait(MessageReceived(
                account="alpha", message_id=1, channel_id=5, author_id=9,
                content="hi",
            ))
            await asyncio.sleep(0.05)
            self.assertEqual(len(ok_inst.seen), 1)


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


class TestLoaderHardening(unittest.TestCase):
    """Round-2: import-time failures, sys.modules hygiene, dir collisions."""

    def test_import_time_error_names_culprit_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            culprit = Path(tmp) / "kaboom.py"
            culprit.write_text("raise RuntimeError('import-time death')\n")
            loader = PluginLoader(Path(tmp))
            with self.assertRaises(PluginError) as ctx:
                loader.discover()
            self.assertIn(str(culprit), str(ctx.exception))
            self.assertIn("import-time death", str(ctx.exception))
            # no half-executed module residue under any synthetic prefix
            residue = [m for m in sys.modules
                       if m.startswith("_mudae_plugroot") and "kaboom" in m]
            self.assertEqual(residue, [])

    def test_rescan_purges_previous_synthetic_modules(self) -> None:
        loader = PluginLoader(FIXTURES)
        loader.discover()
        stale_parent = loader._parent_names[0]
        stale_cls = loader.record("spy").cls
        baseline = len([m for m in sys.modules if m.startswith("_mudae_plugroot")])
        loader.discover()
        loader.discover()
        current = [m for m in sys.modules if m.startswith("_mudae_plugroot")]
        self.assertLessEqual(len(current), baseline)  # bounded, not accumulating
        self.assertNotIn(stale_parent, sys.modules)
        self.assertNotIn(stale_cls.__module__, sys.modules)
        # fresh scan -> fresh class identity, live module registered
        fresh_cls = loader.record("spy").cls
        self.assertIsNot(fresh_cls, stale_cls)
        self.assertIn(fresh_cls.__module__, sys.modules)
        self.assertEqual(loader._parent_names, [loader._parent_names[0]])

    def test_same_plugin_name_in_two_dirs_no_collision(self) -> None:
        source = (
            "from core.plugin_base import PluginBase\n"
            "class TwinPlugin(PluginBase):\n"
            "    name = 'twin'\n"
            "    marker = {__r__!r}\n"
        )
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            f1 = Path(tmp1) / "twin.py"
            f2 = Path(tmp2) / "twin.py"
            f1.write_text(source.format(__r__=1))
            f2.write_text(source.format(__r__=2))
            rec1 = PluginLoader(Path(tmp1)).discover()["twin"]
            rec2 = PluginLoader(Path(tmp2)).discover()["twin"]
            self.assertIsNot(rec1.cls, rec2.cls)
            self.assertEqual(rec1.cls.marker, 1)
            self.assertEqual(rec2.cls.marker, 2)
            self.assertNotEqual(rec1.cls.__module__, rec2.cls.__module__)


class TestRetryAfterFailure(unittest.IsolatedAsyncioTestCase):
    """Round-2: a broken plugin must stay retry-able, never zombie state."""

    async def test_healed_constructor_retries_on_next_attach(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            boom = Path(tmp) / "boom"
            boom.mkdir()
            init = boom / "__init__.py"
            init.write_text(
                "from core.plugin_base import PluginBase\n"
                "class BoomPlugin(PluginBase):\n"
                "    name = 'boom'\n"
                "    def __init__(self):\n"
                "        raise RuntimeError('constructor explodes')\n"
            )
            manager, bus, _ = make_manager(plugin_dir=Path(tmp))
            await manager.attach_account("alpha")
            self.assertFalse(manager._accounts["alpha"]["boom"].enabled)

            init.write_text(  # heal the plugin on disk
                "from core.plugin_base import PluginBase\n"
                "class BoomPlugin(PluginBase):\n"
                "    name = 'boom'\n"
                "    async def handle_event(self, ctx, event):\n"
                "        pass\n"
            )
            manager.discover()  # rescan picks the healed module up
            await manager.attach_account("alpha")  # deactivates old runtimes
            await manager.activate_account("alpha", FakeServices("alpha"))
            healed = manager._accounts["alpha"]["boom"]
            self.assertTrue(healed.enabled)
            self.assertTrue(healed.active)

    async def test_set_enabled_on_instanceless_runtime_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            boom = Path(tmp) / "boom"
            boom.mkdir()
            (boom / "__init__.py").write_text(
                "from core.plugin_base import PluginBase\n"
                "class BoomPlugin(PluginBase):\n"
                "    name = 'boom'\n"
                "    def __init__(self):\n"
                "        raise RuntimeError('constructor explodes')\n"
            )
            manager, bus, _ = make_manager(plugin_dir=Path(tmp))
            await manager.attach_account("alpha")
            await manager.activate_account("alpha", FakeServices("alpha"))
            events: list = []
            bus.subscribe(PluginEnabledChanged, events.append)
            await manager.set_enabled("alpha", "boom", True)  # must not raise
            runtime = manager._accounts["alpha"]["boom"]
            self.assertFalse(runtime.enabled)   # ended disabled, not zombie
            self.assertFalse(runtime.active)
            await asyncio.sleep(0.05)
            self.assertEqual(
                [(e.plugin, e.enabled) for e in events], [("boom", False)]
            )


class TestDispatchHardening(unittest.IsolatedAsyncioTestCase):
    """Round-2: stale snapshots, wedged handlers, mid-flight disable."""

    async def test_event_to_detached_account_not_dispatched(self) -> None:
        manager, bus, _ = make_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices("alpha"))
        spy = manager._accounts["alpha"]["spy"].instance
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=6, channel_id=5, author_id=9, content="x",
        ))
        manager.detach_account("alpha")  # between enqueue and dispatch
        await asyncio.sleep(0.05)
        self.assertEqual(spy.seen, [])

    async def test_wedged_handler_does_not_block_subsequent_events(self) -> None:
        manager, bus, _ = make_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices("alpha"))
        spy = manager._accounts["alpha"]["spy"].instance
        gate = asyncio.Event()
        calls: list = []

        async def handler(ctx, event):
            calls.append(event)
            if len(calls) == 1:
                await gate.wait()  # first handler wedges "forever"

        spy.handle_event = handler
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=7, channel_id=5, author_id=9, content="one",
        ))
        await asyncio.sleep(0.05)  # first dispatch is now wedged
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=8, channel_id=5, author_id=9, content="two",
        ))
        await asyncio.sleep(0.05)
        self.assertEqual(len(calls), 2)  # second event got through
        gate.set()  # unwind the wedged dispatch
        await asyncio.sleep(0.05)
        manager.close()

    async def test_disable_during_inflight_handler(self) -> None:
        manager, bus, _ = make_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices("alpha"))
        spy_runtime = manager._accounts["alpha"]["spy"]
        inst = spy_runtime.instance
        events: list = []
        bus.subscribe(PluginEnabledChanged, events.append)
        started = asyncio.Event()
        finish = asyncio.Event()

        async def handler(ctx, event):
            started.set()
            await finish.wait()  # handler parks mid-dispatch
            inst.seen.append(event)

        inst.handle_event = handler
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=9, channel_id=5, author_id=9, content="x",
        ))
        await started.wait()
        await manager.set_enabled("alpha", "spy", False)  # disable mid-flight
        self.assertFalse(spy_runtime.enabled)
        self.assertIn("on_disable", inst.calls)
        finish.set()  # the in-flight handler completes
        await asyncio.sleep(0.05)
        self.assertEqual(len(inst.seen), 1)
        bus.publish_nowait(MessageReceived(  # further events skipped
            account="alpha", message_id=10, channel_id=5, author_id=9, content="y",
        ))
        await asyncio.sleep(0.05)
        self.assertEqual(len(inst.seen), 1)
        await asyncio.sleep(0.05)
        self.assertEqual(
            [(e.plugin, e.enabled) for e in events], [("spy", False)]
        )

    async def test_allowlist_with_unknown_names_is_ignored(self) -> None:
        manager, _, _ = make_manager()
        await manager.attach_account("alpha", allowed={"spy", "no-such-plugin"})
        self.assertEqual(manager.allowed_names("alpha"), ["spy"])


class TestRound3DispatchHardening(unittest.IsolatedAsyncioTestCase):
    """Round-3: services-None dispatch window, ghost writes past
    deactivation, on_load double-fire."""

    async def test_dispatch_skipped_when_services_popped(self) -> None:
        """deactivate_account pops the services entry BEFORE its hook loop;
        a dispatch task starting inside that window used to pass the
        enabled+active gate, resolve services=None and crash the handler
        with an AttributeError the plugin got blamed for — it must skip."""
        manager, bus, _ = make_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices("alpha"))
        spy = manager._accounts["alpha"]["spy"]
        services_seen: list = []

        async def handler(ctx, event):
            services_seen.append(ctx.services)

        spy.instance.handle_event = handler
        manager._services.pop("alpha")  # simulate the deactivate window:
        # services gone while the runtime still reads enabled+active
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=20, channel_id=5, author_id=9,
            content="x",
        ))
        await asyncio.sleep(0.05)
        self.assertEqual(services_seen, [])   # skipped, not fed services=None
        self.assertEqual(spy.error_count, 0)  # no plugin blamed
        self.assertIsNone(spy.last_error)

    async def test_deactivate_joins_inflight_handler_before_returning(self) -> None:
        """A handler sleeping past the deactivate call must COMPLETE before
        deactivate_account returns — its writes land BEFORE the removal
        cascades the account manager runs next, never after them (ghost
        rows a re-created same-name account would inherit)."""
        manager, bus, _ = make_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices("alpha"))
        inst = manager._accounts["alpha"]["spy"].instance
        started = asyncio.Event()
        finished = asyncio.Event()

        async def handler(ctx, event):
            started.set()
            await asyncio.sleep(0.2)  # sleeping past the deactivate call
            finished.set()            # the "late write"

        inst.handle_event = handler
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=21, channel_id=5, author_id=9,
            content="x",
        ))
        await started.wait()
        await manager.deactivate_account("alpha")  # bounded-join (1s grace)
        self.assertTrue(finished.is_set())
        self.assertIsNone(manager._pending_by_account.get("alpha"))
        self.assertIn("on_unload", inst.calls)  # hooks still ran, after quiesce

    async def test_deactivate_cancels_straggler_after_grace(self) -> None:
        """A handler that outlives the grace window is cancelled: the
        deactivation returns bounded, and the straggler never writes."""
        manager, bus, _ = make_manager(dispatch_grace_s=0.1)
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices("alpha"))
        inst = manager._accounts["alpha"]["spy"].instance
        started = asyncio.Event()
        ghost_writes: list = []

        async def handler(ctx, event):
            started.set()
            await asyncio.sleep(10)           # never finishes within grace
            ghost_writes.append("ghost-row")  # must NEVER land

        inst.handle_event = handler
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=22, channel_id=5, author_id=9,
            content="x",
        ))
        await started.wait()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await manager.deactivate_account("alpha")
        self.assertLess(loop.time() - t0, 2.0)  # grace + cancel, not 10s
        await asyncio.sleep(0.2)
        self.assertEqual(ghost_writes, [])       # cancelled, not landed
        self.assertIsNone(manager._pending_by_account.get("alpha"))

    async def test_concurrent_enable_fires_on_load_once(self) -> None:
        """set_enabled(True) landing on an await point of activate_account
        used to re-fire on_load (both read loaded=False before either set
        it). The loaded flag is now marked BEFORE the hook is awaited."""
        manager, bus, _ = make_manager()
        await manager.attach_account("alpha")
        spy = manager._accounts["alpha"]["spy"]
        inst = spy.instance
        gate = asyncio.Event()
        loads: list = []

        async def slow_on_load(ctx):
            loads.append("on_load")
            await gate.wait()  # activation parked mid-on_load

        inst.on_load = slow_on_load
        activation = asyncio.create_task(
            manager.activate_account("alpha", FakeServices("alpha")))
        while not loads:  # activation is parked inside on_load
            await asyncio.sleep(0.01)
        # the racing toggle cycle that used to double-fire the load hook
        await manager.set_enabled("alpha", "spy", False)
        await manager.set_enabled("alpha", "spy", True)
        self.assertEqual(loads, ["on_load"])  # no second concurrent fire
        gate.set()
        await activation
        self.assertEqual(loads, ["on_load"])
        self.assertEqual(inst.calls.count("on_load"), 0)  # spy hook replaced
        self.assertEqual(inst.calls.count("on_enable"), 2)  # 1st + re-enable


class FakeKv:
    """Dict-backed kv repository incl. prefix deletes."""

    def __init__(self) -> None:
        self.rows: dict = {}

    async def set(self, key, value):
        self.rows[key] = value

    async def get(self, key):
        return self.rows.get(key)

    async def delete(self, key):
        return self.rows.pop(key, None) is not None

    async def delete_prefix(self, prefix):
        doomed = [k for k in self.rows if k.startswith(prefix)]
        for k in doomed:
            self.rows.pop(k)
        return len(doomed)


class TestPluginStateKeys(unittest.IsolatedAsyncioTestCase):
    """Round-2: kv key-space collisions, cascade completeness, big values."""

    async def asyncSetUp(self) -> None:
        from core.plugin_state import PluginStateService
        self.kv = FakeKv()
        self.state = PluginStateService(self.kv)

    async def test_pair_owner_separator_collisions_rejected(self) -> None:
        with self.assertRaises(PluginError):
            await self.state.set_pair("a/p", "x", "k", 1)  # would collide with
        with self.assertRaises(PluginError):               # ("a", "p/x")
            await self.state.set_pair("a", "p/x", "k", 1)
        with self.assertRaises(PluginError):
            await self.state.set_pair("a:b", "p", "k", 1)
        await self.state.set_pair("a", "p", "k", "v")  # the legal pair works
        self.assertEqual(await self.state.get_pair("a", "p", "k"), "v")

    async def test_key_may_contain_separators_stay_scoped(self) -> None:
        await self.state.set_pair("a", "p", "x/y:z", {"n": 1})
        stored = [k for k in self.kv.rows if k.startswith("pstate:account_plugin:")]
        self.assertEqual(stored, ["pstate:account_plugin:a/p:x/y:z"])
        # a colon key in global scope must not bleed into account scope
        await self.state.set_global("account:alice:x", 1)
        self.assertIsNone(await self.state.get_account("alice", "x"))
        self.assertEqual(await self.state.get_global("account:alice:x"), 1)

    async def test_cascade_account_complete_and_selective(self) -> None:
        await self.state.set_account("a", "k1", 1)
        await self.state.set_pair("a", "p", "k2", 2)
        await self.state.set_pair("other", "p", "k3", 3)   # survives
        await self.state.set_plugin("a", "k4", 4)          # survives
        await self.state.set_global("k5", 5)               # survives
        removed = await self.state.cascade_account("a")
        self.assertEqual(removed, 2)
        self.assertEqual(sorted(self.kv.rows),
                         ["pstate:account_plugin:other/p:k3",
                          "pstate:global:k5", "pstate:plugin:a:k4"])

    async def test_cascade_rejects_slash_in_account(self) -> None:
        with self.assertRaises(PluginError):
            await self.state.cascade_account("a/b")  # would wipe ("a","b") pairs

    async def test_large_value_round_trips(self) -> None:
        blob = "x" * (1 << 20)  # 1 MiB: no size cap, but must round-trip
        await self.state.set_pair("a", "p", "big", blob)
        self.assertEqual(await self.state.get_pair("a", "p", "big"), blob)


if __name__ == "__main__":
    unittest.main()
