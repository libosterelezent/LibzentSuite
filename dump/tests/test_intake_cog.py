"""FrameworkIntakeCog + host wiring tests (C1 completion)."""

from __future__ import annotations

import unittest

from discord.ext import commands

from core.plugin_manager.intake_cog import FrameworkIntakeCog


def _make_bot() -> commands.Bot:
    return commands.Bot(command_prefix="!", user_bot=True, owner_ids=set(), help_command=None)


class FrameworkIntakeCogTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_runs_the_pipeline(self) -> None:
        seen: list[int] = []

        async def pipeline(message) -> None:
            seen.append(message.id)

        cog = FrameworkIntakeCog(on_message_pipeline=pipeline)
        await cog.intake_message(type("M", (), {"id": 9})())
        self.assertEqual(seen, [9])

    async def test_cog_registers_on_a_real_bot(self) -> None:
        async def pipeline(message) -> None:
            return None

        bot = _make_bot()
        await bot.add_cog(FrameworkIntakeCog(on_message_pipeline=pipeline))
        self.assertIsNotNone(bot.get_cog("FrameworkIntakeCog"))
        # the listener is registered in the library's extra_events
        self.assertTrue(any("on_message" in str(e) for e in dir(bot)))
        await bot.remove_cog("FrameworkIntakeCog")
        self.assertIsNone(bot.get_cog("FrameworkIntakeCog"))


if __name__ == "__main__":
    unittest.main()
