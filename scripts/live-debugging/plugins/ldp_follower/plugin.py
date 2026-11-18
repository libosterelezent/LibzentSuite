"""LdpFollower — the live-debugging toolkit's follow-side fixture.

A TOOLKIT FIXTURE (scripts/live-debugging/plugins), never discovered by
production boots: the harness stages it next to the real ``plugins/``
tree inside its disposable workdir. It is the FOLLOWER half of the
chain probes, split from ldp_worker (PR review 2026-11-18) because one
plugin playing both roles self-perpetuated:

  * distinct trigger key — the chain-step handler's sends complete as
    plugin=ldp_follower, so they can never re-match the
    ``plugin=ldp_worker`` terminal trigger the run started from (the
    follower's own completion used to fire the chain again, forever);
  * no re-emission — this handler consumes signals and emits NOTHING:
    re-emitting ``chain-done`` from the chain it was started BY seeded
    the next run on the following reconciler sweep. The producer's
    durable emission rides ldp_worker's facade through the outbox;
  * no chat surface — the follower account cannot react to the actor's
    trigger word structurally, instead of by a settings workaround.

Every send goes through ``ctx.services`` (the framework's middleware
seam); the plugin never touches a client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.plugin_base import PluginBase
from core.settings_manager import Setting

if TYPE_CHECKING:
    from core.plugin_base import PluginContext


class LdpFollowerPlugin(PluginBase):
    """The chain-step follower: one paced send per step, never an emission."""

    name = "ldp_follower"
    version = "1.0.0"
    description = "live-debugging follower: chain-step sends without re-emission"

    settings = (Setting("chain_channel", "int", 500, "Channel chain-step results are sent to."),)

    # -------------------------------------------------------------- lifecycle
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.logger.info("ldp_follower loaded")

    async def on_enable(self, ctx: PluginContext) -> None:
        pass

    async def on_disable(self, ctx: PluginContext) -> None:
        pass

    async def on_unload(self, ctx: PluginContext) -> None:
        pass

    # --------------------------------------------------------- chain handler
    async def on_ldp_follower_trigger(self, ctx: PluginContext, payload: dict[str, Any]) -> None:
        """The chain runtime's conventional handler for ldp_follower
        steps (core/workspace/runtime.py::_handler_for).

        Sends one paced result line — and nothing else. No
        ``emit_signal`` call: this handler runs for chains triggered BY
        ``chain-done``, and re-emitting it would start another run on
        the next sweep (PR review 2026-11-18).
        """
        channel = int(ctx.settings.get("chain_channel") or 500)
        await ctx.services.send_message(
            channel, f"ldp follow step done run={payload.get('run_id')}"
        )


__all__ = ["LdpFollowerPlugin"]
