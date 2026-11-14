"""FrameworkVerbsCog tests (C3)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace as NS

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

        async def _send(text):
            sent.append(text)

        ctx = NS(
            bot=bot,
            prefix="!",
            command=bot.get_command("say"),
            message=NS(attachments=[]),
            send=_send,
        )
        await bot.get_command("say")(ctx, text="hello world")
        self.assertEqual(sent, ["hello world"])

    async def test_estop_hits_the_api(self) -> None:
        from core.plugin_manager import commands as commands_mod

        calls: list[tuple] = []
        original = commands_mod.COMMAND_HUB.api_provider

        async def fake_estop_all(surface="api"):
            calls.append(surface)
            return {"stopped": True}

        api = NS(emergency_stop_all=fake_estop_all)
        commands_mod.COMMAND_HUB.api_provider = lambda: api
        try:
            bot = _make_bot()
            await bot.add_cog(FrameworkVerbsCog())
            sent: list[str] = []

            async def _send(text):
                sent.append(text)

            ctx = NS(
                bot=bot,
                prefix="!",
                command=bot.get_command("estop"),
                message=NS(attachments=[]),
                send=_send,
            )
            await bot.get_command("estop")(ctx)
        finally:
            commands_mod.COMMAND_HUB.api_provider = original
        self.assertEqual(calls, ["chat"])
        self.assertEqual(sent, ["emergency stop engaged"])


if __name__ == "__main__":
    unittest.main()
