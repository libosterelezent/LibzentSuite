"""Plugin EMITS/FOLLOWS loader-contract tests (W1).

The loader fails a broken chain declaration at discovery (same
fail-fast convention as COMMANDS) and accepts the clean default
(chains are opt-in: plugins without declarations boot untouched).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.errors import PluginError
from core.plugin_base import PluginBase


def _write_plugin(root: Path, name: str, body: str) -> None:
    (root / f"{name}.py").write_text(body, encoding="utf-8")


class ChainDeclarationLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        from core.plugin_manager.loader import PluginLoader

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.loader = PluginLoader(self.root)

    def test_no_declarations_boot_untouched(self) -> None:
        _write_plugin(
            self.root,
            "plain",
            "from core.plugin_base import PluginBase\n"
            "class Plain(PluginBase):\n"
            "    name = 'plain'\n",
        )
        records = self.loader.discover()
        self.assertIn("plain", records)

    def test_clean_declarations_boot(self) -> None:
        _write_plugin(
            self.root,
            "chained",
            "from core.plugin_base import PluginBase\n"
            "from core.workspace import FollowSpec\n"
            "class Chained(PluginBase):\n"
            "    name = 'chained'\n"
            "    EMITS = ('claim_done',)\n"
            "    FOLLOWS = (FollowSpec(handler='on_claim', signal='chained:claim_done'),)\n"
            "    async def on_claim(self, ctx, payload):\n"
            "        return None\n",
        )
        records = self.loader.discover()
        self.assertIn("chained", records)

    def test_missing_handler_fails_discovery(self) -> None:
        _write_plugin(
            self.root,
            "broken",
            "from core.plugin_base import PluginBase\n"
            "from core.workspace import FollowSpec\n"
            "class Broken(PluginBase):\n"
            "    name = 'broken'\n"
            "    FOLLOWS = (FollowSpec(handler='does_not_exist', task_action='message'),)\n",
        )
        with self.assertRaises(PluginError) as ctx:
            self.loader.discover()
        self.assertIn("does_not_exist", str(ctx.exception))
        self.assertIn("chain declarations", str(ctx.exception))

    def test_namespaced_emit_fails_discovery(self) -> None:
        # plugins emit BARE names; namespacing is added at consumption
        # so one plugin cannot spoof another's signals
        _write_plugin(
            self.root,
            "spoofy",
            "from core.plugin_base import PluginBase\n"
            "class Spoofy(PluginBase):\n"
            "    name = 'spoofy'\n"
            "    EMITS = ('grinder:claim_done',)\n",
        )
        with self.assertRaises(PluginError) as ctx:
            self.loader.discover()
        self.assertIn("namespacing", str(ctx.exception))

    def test_plugin_base_declares_the_surface(self) -> None:
        self.assertEqual(PluginBase.EMITS, ())
        self.assertEqual(PluginBase.FOLLOWS, ())
        self.assertIsInstance(PluginBase.fleet_handler, type(None))


if __name__ == "__main__":
    unittest.main()
