"""Plugin loader: discovery, manifests, instantiation, rejection rules."""
from __future__ import annotations

import unittest

from core.plugin_base import PluginBase
from core.plugin_loader import PluginLoadError, PluginLoader

from .helpers import ROOT, fixture_path


class TestPluginLoader(unittest.TestCase):
    def test_discovers_project_plugins(self) -> None:
        loader = PluginLoader(ROOT / "plugins")
        records = loader.discover()
        self.assertIn("soulcopy", records)
        self.assertEqual(records["soulcopy"].cls.__name__, "SoulcopyPlugin")

    def test_discovers_fixture_plugins(self) -> None:
        loader = PluginLoader(fixture_path("fixtures/plugins"))
        self.assertIn("spy", loader.discover())

    def test_missing_directory_raises(self) -> None:
        with self.assertRaises(PluginLoadError):
            PluginLoader(fixture_path("fixtures/does-not-exist")).discover()

    def test_invalid_name_rejected(self) -> None:
        loader = PluginLoader(fixture_path("fixtures/bad_plugin"))
        with self.assertRaises(PluginLoadError):
            loader.discover()

    def test_create_returns_fresh_plugin_instances(self) -> None:
        loader = PluginLoader(ROOT / "plugins")
        first = loader.create("soulcopy")
        second = loader.create("soulcopy")
        self.assertIsInstance(first, PluginBase)
        self.assertIsNot(first, second)
        self.assertEqual(first.name, "soulcopy")

    def test_unknown_plugin_raises(self) -> None:
        loader = PluginLoader(ROOT / "plugins")
        with self.assertRaises(PluginLoadError):
            loader.create("does-not-exist")

    def test_manifest_shape(self) -> None:
        loader = PluginLoader(ROOT / "plugins")
        manifests = loader.manifests()
        soulcopy = next(m for m in manifests if m["name"] == "soulcopy")
        self.assertEqual(soulcopy["version"], SoulcopyVersion())
        keys = {s["key"] for s in soulcopy["settings"]}
        self.assertIn("trigger", keys)
        self.assertIn("inter_chunk_delay", keys)
        for setting in soulcopy["settings"]:
            self.assertIn(setting["type"], ("str", "int", "float", "bool"))


def SoulcopyVersion() -> str:
    from plugins.soulcopy.plugin import SoulcopyPlugin

    return SoulcopyPlugin.version


if __name__ == "__main__":
    unittest.main()
