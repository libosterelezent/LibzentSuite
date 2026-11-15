"""FrameworkIntakeCog — the framework's ONE listener surface (C1).

The convergence model: the framework's own audit + bus publication ride
a single cog; plugin listeners run ALONGSIDE it (every listener is an
independently scheduled task — the library guarantees coexistence, not
ordering, which is exactly the first-class model the owner locked).

The intake REPLACES the adapter's @client.event handler shadowing: the
same pipeline (self-echo filter → DTO conversion → bus publication →
inbound audit) moved into the library's sanctioned container with real
lifecycle (add/remove with the client). Command dispatch stays the
library's own (the default Bot.on_message → process_commands).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from discord.ext import commands

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["FrameworkIntakeCog"]


class FrameworkIntakeCog(commands.Cog):
    """Audit + bus publication for every message this account sees."""

    def __init__(self, *, on_message_pipeline: Callable[[Any], Any]) -> None:
        """``on_message_pipeline``: the adapter's existing message
        pipeline (echo filter → DTO → cache → bus). The cog owns the
        WIRING; the adapter keeps owning the pipeline semantics."""
        self._pipeline = on_message_pipeline

    @commands.Cog.listener("on_message")
    async def intake_message(self, message: Any) -> None:
        await self._pipeline(message)
