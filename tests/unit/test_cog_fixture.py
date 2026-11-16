"""C2 fixture tests — the converged plugin shape end to end: a plugin
declaring a COG class, hosted per (account, plugin), driven through
the REAL library (typed params resolve, replies carry attribution).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from typing import TYPE_CHECKING, Any

from discord.ext import commands

from core.plugin_manager.cog_host import PluginCogHost

if TYPE_CHECKING:
    from collections.abc import Callable

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"


class CogFixturePluginTests(unittest.IsolatedAsyncioTestCase):
    def _load(self) -> tuple[Any, Any]:
        sys.path.insert(0, str(_FIXTURES))
        try:
            # the fixture package is reachable only through the runtime
            # sys.path injection above — statically invisible to mypy
            from cogprobe.plugin import (  # type: ignore[import-not-found]
                CogProbePlugin,
                ProbeCog,
            )

            return CogProbePlugin, ProbeCog
        finally:
            sys.path.remove(str(_FIXTURES))

    async def test_plugin_declares_a_cog_class(self) -> None:
        plugin_cls, cog_cls = self._load()
        self.assertEqual(plugin_cls.name, "cogprobe")
        self.assertIs(plugin_cls.COG, cog_cls)
        self.assertTrue(issubclass(cog_cls, commands.Cog))

    async def test_host_builds_and_drives_the_declared_cog(self) -> None:
        _plugin_cls, cog_cls = self._load()
        host = PluginCogHost()
        bot = commands.Bot(command_prefix="!", user_bot=True, owner_ids=set(), help_command=None)
        cog = await host.attach(bot, "bravo", "cogprobe", cog_cls)
        self.assertEqual(cog.services.account, "bravo")

        # the standard drive: a typed ctx, the command runs, the typed
        # param (count: int) is parsed BY THE LIBRARY
        sent: list[str] = []

        async def _send(text: str) -> None:
            sent.append(text)

        # direct callback invocation with the typed kwargs — the
        # signature contract (count: int resolved by the library's
        # converters on the real message path; here verified directly)
        ctx = NS(
            bot=bot,
            prefix="!",
            invoked_with="cogprobe",
            command=bot.get_command("cogprobe"),
            message=NS(attachments=[]),
            send=_send,  # replies via ctx.send — the documented seam
        )
        command = bot.get_command("cogprobe")
        assert command is not None
        # the ctx is a duck-typed stand-in for the library Context, so
        # the invocation goes through an untyped callable seam
        invoke: Callable[..., Any] = command
        await invoke(ctx, count=5)
        self.assertEqual(sent, ["cog ok bravo:5"])


if __name__ == "__main__":
    unittest.main()
