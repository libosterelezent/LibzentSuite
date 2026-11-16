"""COG-declaration loader tests (C2)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.errors import PluginError


def _write(root: Path, name: str, body: str) -> None:
    (root / f"{name}.py").write_text(body, encoding="utf-8")


class CogDeclarationLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        from core.plugin_manager.loader import PluginLoader

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.loader = PluginLoader(self.root)

    def test_valid_cog_boots(self) -> None:
        _write(
            self.root, "goodcog",
            "from core.plugin_base import PluginBase\n"
            "from discord.ext import commands\n"
            "class GoodCog(commands.Cog):\n"
            "    @commands.command(name='hi')\n"
            "    async def hi(self, ctx):\n"
            "        await ctx.send('hi')\n"
            "class GoodPlugin(PluginBase):\n"
            "    name = 'goodcog'\n"
            "    COG = GoodCog\n",
        )
        records = self.loader.discover()
        self.assertIn("goodcog", records)

    def test_instance_not_class_fails(self) -> None:
        _write(
            self.root, "badcog",
            "from core.plugin_base import PluginBase\n"
            "from discord.ext import commands\n"
            "class C(commands.Cog):\n"
            "    pass\n"
            "class BadPlugin(PluginBase):\n"
            "    name = 'badcog'\n"
            "    COG = C()  # instance, not the class\n",
        )
        with self.assertRaises(PluginError) as ctx:
            self.loader.discover()
        self.assertIn("must be a Cog subclass", str(ctx.exception))

    def test_non_cog_class_fails(self) -> None:
        _write(
            self.root, "notcog",
            "from core.plugin_base import PluginBase\n"
            "class NotPlugin(PluginBase):\n"
            "    name = 'notcog'\n"
            "    COG = dict  # a class, but not a Cog\n",
        )
        with self.assertRaises(PluginError) as ctx:
            self.loader.discover()
        self.assertIn("must subclass commands.Cog", str(ctx.exception))

    def test_absent_cog_still_boots_during_migration(self) -> None:
        _write(
            self.root, "legacy",
            "from core.plugin_base import PluginBase\n"
            "class Legacy(PluginBase):\n"
            "    name = 'legacy'\n",
        )
        records = self.loader.discover()
        self.assertIn("legacy", records)


if __name__ == "__main__":
    unittest.main()
