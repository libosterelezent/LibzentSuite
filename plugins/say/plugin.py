"""Say plugin: sends a provided message to a chosen channel.

The first REAL production plugin (the framework ships none by design —
this is the operator's first drop-in). One capability, done safely:

  * ``<prefix>say <channel_id> <text...>`` — self-command that sends
    ``text`` to ``channel_id`` through the SAME paced pipeline as all
    plugin traffic (ctx.services.send_message — never a raw client),
  * ``on_say`` dashboard-UI action does the same from the dashboard,
  * ``FlowBlocked`` safety: a gone/forbidden channel aborts ONE clean
    log line — the plugin contract (core/plugin_base.py docstring).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from core.errors import FlowBlocked
from core.event_bus import Event
from core.plugin_base import (
    ArgSpec,
    CommandContext,
    CommandSpec,
    PluginBase,
    PluginContext,
)
from core.settings_manager import Setting

if TYPE_CHECKING:
    from core.dashboard_ui.builders import DashboardUIToolset

LOG = logging.getLogger("libzent.say")


class SayPlugin(PluginBase):
    """Send a provided message to a chosen channel (paced, audited)."""

    name = "say"
    version = "1.0.0"
    description = "sends a provided message to a chosen channel"

    #: no settings — the channel + text arrive per invocation
    settings: tuple = ()

    def __init__(self) -> None:
        self._ctx: Optional[PluginContext] = None
        #: (channel_id, text) pairs sent this activation — ui_stats feed
        self._sent: List[Dict[str, Any]] = []

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
        return  # say is command-driven; it never reacts to traffic

    # ------------------------------------------------------------ the action
    async def _say(self, ctx: PluginContext, channel_id: int,
                   text: str) -> str:
        """Validate, send through the paced pipeline, remember the send.

        Returns the operator reply; raises nothing on FlowBlocked (one
        clean abort line is the plugin's own contract).
        """
        try:
            ctx.services.validate_channel(channel_id)
        except FlowBlocked as exc:
            ctx.logger.info("say: channel %s unusable — %s",
                            channel_id, exc)
            return f"channel unusable: {exc}"
        try:
            await ctx.services.send_message(channel_id, text)
        except FlowBlocked as exc:
            ctx.logger.info("say: send to %s blocked — %s",
                            channel_id, exc)
            return f"send blocked: {exc}"
        self._sent.append({"channel_id": channel_id,
                           "text": text[:80]})
        where = ctx.services.describe_channel(channel_id)
        ctx.logger.info("say: delivered to %s (%s)",
                        where, channel_id)
        return f"sent to {where}"

    # ------------------------------------------------------- self-commands
    async def _cmd_say(self, cmd: CommandContext,
                       args: List[str]) -> str:
        """``say <channel_id> <text...>`` handler (bound per account)."""
        if not cmd.services or len(args) < 2:
            return "usage: say <channel_id> <text...>"
        try:
            channel_id = int(args[0])
        except ValueError:
            return "usage: say <channel_id> <text...> (id must be a number)"
        # reuse the executor's context: same services facade as events
        return await self._say(cmd, channel_id, " ".join(args[1:]))

    COMMANDS = (
        CommandSpec(
            name="say",
            help="Send a message to a channel",
            usage="<channel_id> <text...>",
            args=(
                ArgSpec("channel_id", kind=int),
                ArgSpec("text", greedy=True),
            ),
            # bound to the per-account instance at collection time
            handler=_cmd_say,
            category="Utilities",
        ),
    )

    # ------------------------------------------------------ dashboard UI
    async def ui_stats(self, ctx: "PluginContext"):
        from core.plugin_base import StatCard

        return (
            StatCard(label="say sends",
                     value=str(len(self._sent)),
                     sub="this activation",
                     level="ok" if self._sent else ""),
        )

    def initialize_dashboard_ui(self, ui: "DashboardUIToolset") -> None:
        """A small page: channel + text form, one Send button."""
        page = (
            ui.Page(component_id="say")
            .set_title("Say")
            .set_description("Send a provided message to a chosen "
                             "channel (paced + audited).")
            .add_section(
                ui.Section(component_id="say-sec")
                .add_component(
                    ui.Form(component_id="say-form")
                    .add_component(ui.NumberInput(component_id="say-channel")
                                   .set_label("Channel ID")
                                   .set_placeholder("123456789"))
                    .add_component(ui.Textarea(component_id="say-text")
                                   .set_label("Message")
                                   .set_placeholder("text to send"))
                    .add_component(
                        ui.Button(component_id="say-send")
                        .set_label("Send")
                        .set_variant("primary")
                        .set_action(self._ui_say))))
        )
        self.attach_dashboard_ui(page)

    async def _ui_say(self, ctx: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Dashboard action: same path as the self-command."""
        channel = payload.get("say-channel")
        text = str(payload.get("say-text") or "").strip()
        if not channel or not text:
            return {"success": False,
                    "field_errors": {
                        "say-channel" if not channel else "say-text":
                            "required" }}
        pctx = self._ctx
        if pctx is None:
            return {"success": False, "message": "plugin not active"}
        reply = await self._say(pctx, int(channel), text)
        ok = reply.startswith("sent to")
        return {"success": ok, "message": reply}


__all__ = ["SayPlugin"]
