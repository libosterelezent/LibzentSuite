"""Roll plugin: the minimal reference plugin (spec §4's future 'Roll').

Demonstrates the intended shape of action plugins:
  * an owner trigger word fires ONE configured command,
  * the send goes through ``ctx.services.send_message`` — the task pipeline
    applies pacing/jitter/priority/audit, so the plugin never self-throttles,
  * a plugin-level rate policy (``ratelimit set message 4.0 plugin roll``)
    shapes its pacing without touching any code.

Copy this file as the template for snipe/collector-style plugins.
"""
from __future__ import annotations

from core.event_bus import Event, MessageReceived
from core.plugin_base import PluginBase, PluginContext
from core.priority import Priority
from core.settings_manager import Setting


class RollPlugin(PluginBase):
    """Send a configurable Mudae command when the owner types the trigger."""

    name = "roll"
    version = "1.0.0"
    description = "Reference action plugin: fire one command on an owner trigger."

    settings = (
        Setting("trigger", "str", "roll", "Command word that fires the roll (owners only)."),
        Setting("command", "str", "$m", "Command sent to the triggering channel."),
        Setting("confirm_reply", "bool", False,
                "Log the sent message id after a successful send."),
    )

    async def on_load(self, ctx: PluginContext) -> None:
        ctx.logger.info("roll loaded (trigger=%r command=%r)",
                        ctx.settings["trigger"], ctx.settings["command"])

    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        """Owner trigger -> one send through the pipeline; nothing else."""
        if not isinstance(event, MessageReceived):
            return
        if event.content.strip() != ctx.settings["trigger"]:
            return
        if event.author_id not in ctx.services.credentials.owner_user_ids:
            return  # quiet: non-owner triggers are simply ignored
        # validate BEFORE acting — a vanished channel aborts cleanly
        ctx.services.validate_channel(event.channel_id)
        sent = await ctx.services.send_message(
            event.channel_id, ctx.settings["command"], priority=Priority.NORMAL
        )
        if ctx.settings["confirm_reply"] and sent is not None:
            ctx.logger.info("roll sent (message id %s)", getattr(sent, "id", "?"))


__all__ = ["RollPlugin"]
