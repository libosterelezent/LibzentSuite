"""Ping plugin: sends a provided message to a chosen channel.

The first REAL production plugin (the framework ships none by design —
this is the operator's first drop-in). One capability, done safely:

  * ``<prefix>ping <channel_id> <text...>`` — self-command that sends
    ``text`` to ``channel_id`` through the SAME paced pipeline as all
    plugin traffic (ctx.services.send_message — never a raw client),
  * ``on_ping`` dashboard-UI action does the same from the dashboard,
  * ``FlowBlocked`` safety: a gone/forbidden channel aborts ONE clean
    log line — the plugin contract (core/plugin_base.py docstring).

Named ``ping`` (not ``say``) because the framework reserves ``say`` as
a retired RemoteControl mention verb — a plugin re-declaring it is
dropped at collection time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from core.errors import FlowBlocked
from core.plugin_base import (
    ArgSpec,
    CommandContext,
    CommandSpec,
    PluginBase,
    PluginContext,
)

if TYPE_CHECKING:
    from core.dashboard_ui.builders import DashboardUIToolset
    from core.event_bus import Event

LOG = logging.getLogger("libzent.ping")


class PingPlugin(PluginBase):
    """Send a provided message to a chosen channel (paced, audited)."""

    name = "ping"
    version = "1.1.0"
    description = "sends a provided message to a chosen channel"

    #: no settings — the channel + text arrive per invocation
    settings: tuple = ()

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        #: (channel_id, text) pairs sent this activation — ui_stats feed
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
        """Validate, send through the paced pipeline, remember the send.

        Returns the operator reply; raises nothing on FlowBlocked (one
        clean abort line is the plugin's own contract).
        """
        try:
            ctx.services.validate_channel(channel_id)
        except FlowBlocked as exc:
            ctx.logger.info("ping: channel %s unusable — %s", channel_id, exc)
            return f"channel unusable: {exc}"
        try:
            await ctx.services.send_message(channel_id, text)
        except FlowBlocked as exc:
            ctx.logger.info("ping: send to %s blocked — %s", channel_id, exc)
            return f"send blocked: {exc}"
        self._sent.append({"channel_id": channel_id, "text": text[:80]})
        where = ctx.services.describe_channel(channel_id)
        ctx.logger.info("ping: delivered to %s (%s)", where, channel_id)
        return f"sent to {where}"

    # ------------------------------------------------------- self-commands
    async def _cmd_ping(self, cmd: CommandContext, args: list[str]) -> str:
        """``ping <channel_id> <text...>`` handler (bound per account)."""
        if not cmd.services or len(args) < 2:
            return "usage: ping <channel_id> <text...>"
        try:
            channel_id = int(args[0])
        except ValueError:
            return "usage: ping <channel_id> <text...> (id must be a number)"
        # reuse the executor's context: same services facade as events
        return await self._ping(cmd, channel_id, " ".join(args[1:]))

    COMMANDS = (
        CommandSpec(
            name="ping",
            help="Send a message to a channel",
            usage="<channel_id> <text...>",
            args=(
                ArgSpec("channel_id", kind=int),
                ArgSpec("text", greedy=True),
            ),
            # bound to the per-account instance at collection time
            handler=_cmd_ping,
            category="Utilities",
        ),
    )

    # ------------------------------------------------------ dashboard UI
    async def ui_stats(self, ctx: PluginContext):
        from core.plugin_base import StatCard

        return (
            StatCard(
                label="ping sends",
                value=str(len(self._sent)),
                sub="this activation",
                level="ok" if self._sent else "",
            ),
        )

    def initialize_dashboard_ui(self, ui: DashboardUIToolset) -> None:
        """A small page: channel + text form, one Send button."""
        page = (
            ui.Page(component_id="ping")
            .set_title("Ping")
            .set_icon("M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0V8zM12 18v4")
            .set_description("Send a provided message to a chosen channel (paced + audited).")
            .add_section(
                ui.Section(component_id="ping-sec").add_component(
                    ui.Form(component_id="ping-form")
                    .set_description(
                        "The message rides the SAME paced pipeline as all plugin traffic."
                    )
                    .set_submit_action(self._ui_ping)
                    .set_submit_label("Send")
                    .set_close_on_submit(False)
                    .add_fields(
                        ui.TextInput(component_id="ping-channel")
                        .set_label("Channel ID")
                        .set_placeholder("123456789"),
                        ui.Textarea(component_id="ping-text")
                        .set_label("Message")
                        .set_placeholder("text to send"),
                    )
                )
            )
        )
        layout = ui.Layout()
        layout.add_page(page)
        self.attach_dashboard_ui(layout)

    async def _ui_ping(self, ctx: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Dashboard action: same path as the self-command."""
        channel = payload.get("ping-channel")
        text = str(payload.get("ping-text") or "").strip()
        if not channel or not text:
            return {
                "success": False,
                "field_errors": {"ping-channel" if not channel else "ping-text": "required"},
            }
        pctx = self._ctx
        if pctx is None:
            return {"success": False, "message": "plugin not active"}
        reply = await self._ping(pctx, int(channel), text)
        ok = reply.startswith("sent to")
        return {"success": ok, "message": reply}


__all__ = ["PingPlugin"]
