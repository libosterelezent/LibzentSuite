"""Ping plugin: sends a provided message to a chosen channel.

The first REAL production plugin (the framework ships none by design —
this is the operator's first drop-in). One capability, done safely:

  * ``<prefix>ping <channel_id> <text...>`` — self-command that sends
    ``text`` to ``channel_id`` through the SAME paced pipeline as all
    plugin traffic (ctx.services.send_message — never a raw client),
  * ``FlowBlocked`` safety: a gone/forbidden channel aborts ONE clean
    log line — the plugin contract (core/plugin_base.py docstring).
Named ``ping`` (not ``say``) because the framework cog reserves
``say`` — a plugin re-declaring it is a collision, not an override.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from core.errors import FlowBlocked
from core.plugin_base import PluginBase, PluginContext

if TYPE_CHECKING:
    from core.event_bus import Event
    from core.settings_manager import Setting

LOG = logging.getLogger("libzent.ping")


class PingPlugin(PluginBase):
    """Send a provided message to a chosen channel (paced, audited)."""

    name = "ping"
    version = "1.1.0"
    description = "sends a provided message to a chosen channel"

    #: no settings — the channel + text arrive per invocation
    settings: ClassVar[tuple[Setting, ...]] = ()

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        #: (channel_id, text) pairs sent this activation
        self._sent: list[dict[str, Any]] = []

    # ------------------------------------------------------------ lifecycle
    async def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def on_disable(self, ctx: PluginContext) -> None:
        pass

    async def on_unload(self, ctx: PluginContext) -> None:
        self._ctx = None
        self._sent.clear()

    # ---------------------------------------------------------------- events
    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        return  # ping is command-driven; it never reacts to traffic

    # ------------------------------------------------------------ the action
    async def _ping(self, ctx: PluginContext, channel_id: int, text: str) -> str:
        """Send ``text`` to ``channel_id`` through the paced pipeline.

        Returns the operator reply; raises nothing on FlowBlocked (one
        clean abort line is the plugin's own contract). The command face
        returns as a COG once the host injects the paced facade (roadmap);
        tests exercise this behavior directly meanwhile.
        """
        services = ctx.services
        try:
            services.validate_channel(channel_id)
        except FlowBlocked as exc:
            ctx.logger.info("ping: channel %s unusable — %s", channel_id, exc)
            return f"channel unusable: {exc}"
        try:
            await services.send_message(channel_id, text)
        except FlowBlocked as exc:
            ctx.logger.info("ping: send to %s blocked — %s", channel_id, exc)
            return f"send blocked: {exc}"
        self._sent.append({"channel_id": channel_id, "text": text[:80]})
        where = services.describe_channel(channel_id)
        ctx.logger.info("ping: delivered to %s (%s)", where, channel_id)
        return f"sent to {where}"


__all__ = ["PingPlugin"]
