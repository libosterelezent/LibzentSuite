"""Soulcopy plugin: mirrors your Mudae soulmate list between accounts.

Flow (behavior ported from the audited original onto the task pipeline):
  1. an OWNER sends the trigger word (default 'slcopy') in any channel,
  2. the plugin requests the export by sending '$slud-s' in the TRIGGER
     CHANNEL and collects the Mudae DM replies until dm_read_timeout passes
     with no new page,
  3. pages are collapsed + chunked (see textutils) and sent back to the
     triggering channel with inter_chunk_delay between chunks — every send
     goes through ctx.services (task scheduler -> rate pacing -> audit).

Every tunable is a declared Setting; the Mudae id and the allowed owner ids
come from credentials (.env) — nothing is hardcoded.

Flow safety (unchanged contract):
  The services/cache raise FlowBlocked subclasses when this flow's target is
  unusable; the plugin catches FlowBlocked around its WHOLE flow:
    * TargetGone   — the triggering channel is not in the account's cache
      (deleted, evicted, or never visible); validated before anything is
      sent, so an abort can never produce partial chunks.
    * AccessDenied — Discord refused a send (permissions revoked / vanished
      target).
  On either, exactly ONE warning line is logged
  (``flow aborted for <account>: <reason>``) and the flow returns without
  sending anything further — no re-raise, no partial output. Other accounts
  keep running either way: per-account isolation is the dispatcher's job,
  failing clean and quiet is the plugin's.
"""
from __future__ import annotations

import asyncio

from core.errors import FlowBlocked
from core.event_bus import Event, MessageReceived
from core.plugin_base import PluginBase, PluginContext
from core.priority import Priority
from core.settings_manager import Setting

from .textutils import DEFAULT_PREFIX, build_soulcopy_chunks


def flow_block_reason(exc: BaseException) -> str:
    """Human-readable reason for a flow abort (pure -> unit-testable).

    FlowBlocked (and subclasses) carry a curated ``reason``; anything else is
    rendered generically as ``"<TypeName>: <message>"``.
    """
    if isinstance(exc, FlowBlocked):
        return exc.reason
    return f"{type(exc).__name__}: {exc}"


class SoulcopyPlugin(PluginBase):
    """Export Mudae's soulmate list as chunks ready for '$soulcopy' import."""

    name = "soulcopy"
    version = "2.0.0"
    description = "Export your Mudae soulmate list and re-import it elsewhere."

    settings = (
        Setting("trigger", "str", "slcopy", "Command word that starts the copy (owners only)."),
        Setting("dm_read_timeout", "float", 10.0, "Idle seconds before DM reading stops.",
                min_value=0.5, max_value=120.0),
        Setting("inter_chunk_delay", "float", 10.0, "Seconds between sending chunks.",
                min_value=0.0, max_value=120.0),
        Setting("max_chunk_length", "int", 2000, "Hard Discord message limit.",
                min_value=50, max_value=2000),
        Setting("export_command", "str", "$slud-s", "Mudae command that DMs the soulmate list."),
        Setting("import_prefix", "str", DEFAULT_PREFIX, "Prefix used for the produced chunks."),
    )

    # -------------------------------------------------------------- lifecycle
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.logger.info("soulcopy loaded (trigger=%r)", ctx.settings["trigger"])

    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        """React to the trigger message; ignore everything else fast."""
        if not isinstance(event, MessageReceived):
            return
        if event.content.strip() != ctx.settings["trigger"]:
            return
        if event.author_id not in ctx.services.credentials.owner_user_ids:
            ctx.logger.warning(
                "ignoring trigger from non-owner %s on account %s (if this is you, "
                "add the id to OWNER_USER_IDS in .env)", event.author_id, ctx.account,
            )
            return
        try:
            await self._run_copy(ctx, event.channel_id)
        except FlowBlocked as exc:
            # Flow safety contract: one clean line for THIS account, then stop
            # — no partial chunks, no re-raise (the dispatcher would only see
            # a crash; the flow itself is simply over). See module docstring.
            ctx.logger.warning("flow aborted for %s: %s", ctx.account, flow_block_reason(exc))

    # ------------------------------------------------------------------- core
    async def _run_copy(self, ctx: PluginContext, trigger_channel: int) -> None:
        """Drive the full copy flow for one trigger.

        Raises FlowBlocked (TargetGone/AccessDenied) when the target channel
        is unusable — handle_event renders the abort log line from it.
        """
        # cache-aware: validate BEFORE acting so a gone channel aborts the
        # flow before a single message leaves the account
        ctx.services.validate_channel(trigger_channel)
        mudae_id = ctx.services.credentials.mudae_bot_id
        dm_channel = await ctx.services.open_dm(mudae_id)

        async def _request_export() -> None:
            await ctx.services.send_message(
                trigger_channel, ctx.settings["export_command"], priority=Priority.NORMAL
            )

        # ask Mudae for the list (in the trigger channel), reading the DM
        # replies as they arrive — same gather structure as the original
        ctx.logger.info("requesting soulmate export from Mudae")
        pages, _ = await asyncio.gather(
            self._collect_pages(ctx, mudae_id, dm_channel),
            _request_export(),
        )
        ctx.logger.info("collected %d soulmate page(s)", len(pages))

        chunks = build_soulcopy_chunks(
            [p.content for p in pages],
            max_chunk_length=ctx.settings["max_chunk_length"],
            prefix=ctx.settings["import_prefix"],
        )
        for chunk in chunks:
            if ctx.settings["inter_chunk_delay"] > 0:
                await asyncio.sleep(ctx.settings["inter_chunk_delay"])
            await ctx.services.send_message(trigger_channel, chunk, priority=Priority.LOW)
        target = ctx.services.describe_channel(trigger_channel)
        ctx.logger.info("sent %d chunk(s) back to %s", len(chunks), target)

    async def _collect_pages(self, ctx: PluginContext, mudae_id: int, dm_channel: int):
        """Collect Mudae DM pages until the idle timeout expires."""
        return await ctx.services.collect_messages(
            check=lambda e: e.author_id == mudae_id and e.channel_id == dm_channel,
            idle_timeout=ctx.settings["dm_read_timeout"],
        )


__all__ = ["SoulcopyPlugin", "flow_block_reason"]
