"""LdpWorker — the live-debugging toolkit's workload plugin.

A TOOLKIT FIXTURE (scripts/live-debugging/plugins), never discovered by
production boots: the harness stages it next to the real ``plugins/``
tree inside its disposable workdir so the live pass has traffic to
drive. It exists to exercise, through the REAL paced pipeline:

  * event intake — an owner trigger word starts a paced burst of
    sends (tasks, scheduler, rate pacing, audit, history),
  * chain handlers — ``on_ldp_worker_trigger`` is the conventional
    follow-handler name the chain runtime invokes (core/workspace/
    runtime.py::_handler_for), so chains have something real to call,
  * durable signals — the handler emits ``chain-done`` through the
    outbox, feeding the signal-triggered chain path.

Every send goes through ``ctx.services`` (the framework's middleware
seam); the plugin never touches a client.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from core.event_bus import Event, MessageReceived
from core.plugin_base import PluginBase
from core.settings_manager import Setting

if TYPE_CHECKING:
    from core.plugin_base import PluginContext

LOG = logging.getLogger("libzent.ldp_worker")


class LdpWorkerPlugin(PluginBase):
    """Paced bursts on an owner trigger word + chain-step handler."""

    name = "ldp_worker"
    version = "1.0.0"
    description = "live-debugging workload: paced bursts, chain handlers, durable signals"

    #: declared emissions (bare names — namespacing is added at consumption)
    EMITS: ClassVar[tuple[str, ...]] = ("chain-done",)

    settings = (
        Setting("trigger", "str", "ldp-go", "Owner word that starts a paced burst."),
        Setting("burst", "int", 2, "Messages per triggered burst.", 1, 10),
        Setting("chain_channel", "int", 500, "Channel chain-step results are sent to."),
    )

    # -------------------------------------------------------------- lifecycle
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.logger.info("ldp_worker loaded (trigger=%r)", ctx.settings.get("trigger"))

    async def on_enable(self, ctx: PluginContext) -> None:
        pass

    async def on_disable(self, ctx: PluginContext) -> None:
        pass

    async def on_unload(self, ctx: PluginContext) -> None:
        pass

    # ----------------------------------------------------------------- events
    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        """Owner trigger word ``<trigger> [count]`` → paced burst of sends."""
        if not isinstance(event, MessageReceived):
            return
        content = (event.content or "").strip()
        trigger = str(ctx.settings.get("trigger") or "ldp-go")
        if not content.startswith(trigger):
            return
        if event.author_id not in ctx.services.credentials.owner_user_ids:
            LOG.warning("ldp_worker: ignoring non-owner trigger from %s", event.author_id)
            return
        words = content.split()
        try:
            count = int(words[1]) if len(words) > 1 else int(ctx.settings.get("burst") or 2)
        except ValueError:
            count = int(ctx.settings.get("burst") or 2)
        count = max(1, min(10, count))
        for i in range(count):
            await ctx.services.send_message(event.channel_id, f"ldp tick {i + 1}/{count}")

    # --------------------------------------------------------- chain handler
    async def on_ldp_worker_trigger(self, ctx: PluginContext, payload: dict[str, Any]) -> None:
        """The chain runtime's conventional handler for ldp_worker steps.

        Sends one paced result line, then emits the ``chain-done``
        signal through the durable outbox (persisted before the
        emission acknowledges; delivered by the workspace reconciler).
        """
        channel = int(ctx.settings.get("chain_channel") or 500)
        await ctx.services.send_message(channel, f"ldp chain step done run={payload.get('run_id')}")
        await ctx.services.emit_signal("chain-done", {"run_id": payload.get("run_id")})


__all__ = ["LdpWorkerPlugin"]
