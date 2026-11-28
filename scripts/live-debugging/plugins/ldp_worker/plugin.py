"""LdpWorker — the live-debugging toolkit's workload plugin.

A TOOLKIT FIXTURE (scripts/live-debugging/plugins), never discovered by
production boots: the harness stages it next to the real ``plugins/``
tree inside its disposable workdir so the live pass has traffic to
drive. It is the ACTOR half of the chain probes — it exists to
exercise, through the REAL paced pipeline:

  * event intake — an owner trigger word starts a paced burst of
    sends (tasks, scheduler, rate pacing, audit, history); the burst's
    task completions are the terminal triggers that fire chains,
  * durable signals — the facade's ``emit_signal`` emits ``chain-done``
    through the outbox (persisted before the emission acknowledges;
    delivered by the workspace reconciler), feeding the
    signal-triggered chain path.

The chain-step FOLLOWER role lives in ldp_follower since PR review
2026-11-18: a step handler on this plugin re-emitted ``chain-done`` and
its sends re-matched ldp_worker's own terminal trigger — both halves
on one plugin self-perpetuated runs.

Every send goes through ``ctx.services`` (the framework's middleware
seam); the plugin never touches a client.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from libzentsuite.core.event_bus import Event, MessageReceived
from libzentsuite.core.plugin_base import PluginBase
from libzentsuite.core.settings_manager import Setting

if TYPE_CHECKING:
    from libzentsuite.core.plugin_base import PluginContext

LOG = logging.getLogger("libzent.ldp_worker")


class LdpWorkerPlugin(PluginBase):
    """Paced bursts on an owner trigger word (the chain probes' ACTOR)."""

    name = "ldp_worker"
    version = "1.0.0"
    description = "live-debugging workload: paced bursts, durable signals"

    #: declared emissions (bare names — namespacing is added at
    #: consumption); the facade's emit_signal is the durable producer
    #: of ``chain-done`` — no handler on this plugin emits it
    EMITS: ClassVar[tuple[str, ...]] = ("chain-done",)

    settings = (
        Setting("trigger", "str", "ldp-go", "Owner word that starts a paced burst."),
        Setting("burst", "int", 2, "Messages per triggered burst.", 1, 10),
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
        # token boundary: "ldp-going 3" must not fire the "ldp-go"
        # trigger (a bare startswith matched prefixes)
        if content != trigger and not content.startswith(trigger + " "):
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


__all__ = ["LdpWorkerPlugin"]
