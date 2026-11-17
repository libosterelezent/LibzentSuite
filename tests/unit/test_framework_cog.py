"""FrameworkVerbsCog tests (C3)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace as NS
from typing import Any

from discord.ext import commands

from core.plugin_manager.framework_cog import FrameworkVerbsCog


def _make_bot() -> commands.Bot:
    return commands.Bot(command_prefix="!", user_bot=True, owner_ids=set(), help_command=None)


class FrameworkVerbsCogTests(unittest.IsolatedAsyncioTestCase):
    async def test_verbs_register_as_a_real_cog(self) -> None:
        bot = _make_bot()
        await bot.add_cog(FrameworkVerbsCog())
        for name in ("pg", "say", "status", "estop", "adminlist"):
            self.assertIsNotNone(bot.get_command(name), name)

    async def test_say_replies_via_ctx_send(self) -> None:
        bot = _make_bot()
        await bot.add_cog(FrameworkVerbsCog())
        sent: list[str] = []

        async def _send(text: str) -> None:
            sent.append(text)

        ctx: Any = NS(
            bot=bot,
            prefix="!",
            command=bot.get_command("say"),
            message=NS(attachments=[]),
            send=_send,
        )
        say = bot.get_command("say")
        assert say is not None
        await say(ctx, text="hello world")
        self.assertEqual(sent, ["hello world"])

    async def test_estop_hits_the_api(self) -> None:
        from core.plugin_manager import commands as commands_mod

        calls: list[str] = []
        original = commands_mod.COMMAND_HUB.api_provider

        async def fake_estop_all(surface: str = "api") -> dict[str, bool]:
            calls.append(surface)
            return {"stopped": True}

        api = NS(emergency_stop_all=fake_estop_all)
        commands_mod.COMMAND_HUB.api_provider = lambda: api
        try:
            bot = _make_bot()
            await bot.add_cog(FrameworkVerbsCog())
            sent: list[str] = []

            async def _send(text: str) -> None:
                sent.append(text)

            ctx: Any = NS(
                bot=bot,
                prefix="!",
                command=bot.get_command("estop"),
                message=NS(attachments=[]),
                send=_send,
            )
            estop = bot.get_command("estop")
            assert estop is not None
            await estop(ctx)
        finally:
            commands_mod.COMMAND_HUB.api_provider = original
        self.assertEqual(calls, ["chat"])
        self.assertEqual(sent, ["emergency stop engaged"])

    async def test_display_verbs_hit_real_api_methods(self) -> None:
        """pg/ratelimit/plugins must call methods ControlAPI actually has.

        Regression: the C3 port pointed them at ratelimit_show() /
        plugins_list(), which never existed on ControlAPI — every
        invocation AttributeError'd. Found by the mypy-strict pass
        (2026-11-15); this pins the corrected names.
        """
        from core.plugin_manager import commands as commands_mod

        called: list[str] = []
        original = commands_mod.COMMAND_HUB.api_provider

        async def _stats() -> dict[str, float]:
            called.append("ratelimit_stats")
            return {"floor": 1.5}

        async def _manifests() -> list[dict[str, str]]:
            called.append("plugins_manifests")
            return [{"name": "ping"}]

        api = NS(ratelimit_stats=_stats, plugins_manifests=_manifests)
        commands_mod.COMMAND_HUB.api_provider = lambda: api
        try:
            bot = _make_bot()
            await bot.add_cog(FrameworkVerbsCog())
            sent: list[str] = []

            async def _send(text: str) -> None:
                sent.append(text)

            for verb in ("pg", "ratelimit", "plugins"):
                ctx: Any = NS(
                    bot=bot,
                    prefix="!",
                    command=bot.get_command(verb),
                    message=NS(attachments=[]),
                    send=_send,
                )
                verb_command = bot.get_command(verb)
                assert verb_command is not None
                await verb_command(ctx)
        finally:
            commands_mod.COMMAND_HUB.api_provider = original
        self.assertEqual(called.count("ratelimit_stats"), 2)  # pg + ratelimit
        self.assertEqual(called.count("plugins_manifests"), 1)
        self.assertEqual(len(sent), 3, sent)


if __name__ == "__main__":
    unittest.main()
