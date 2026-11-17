"""Soulcopy workload plugin — SIMULATION FIXTURE (tests/fixtures/sim_plugins).

Removed from the production ``plugins/`` tree; retained here ONLY as the
multi-step workload the full-stack simulation scenarios drive (paced
chunked sends over the real scheduler/limiter pipeline). It is never
discovered by production boots — only stacks pointed at this fixture
directory load it.

Flow (behavior ported from the audited original onto the task pipeline):
  1. an OWNER sends the trigger word (default 'export') in any channel,
  2. the plugin requests the export by sending '$dump' in the TRIGGER
     CHANNEL and collects the the_bot DM replies until dm_read_timeout passes
     with no new page,
  3. pages are collapsed + chunked (see textutils) and sent back to the
     triggering channel with inter_chunk_delay between chunks — every send
     goes through ctx.services (task scheduler -> rate pacing -> audit).

Every tunable is a declared Setting; the the_bot id and the allowed owner ids
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
import contextlib

from core.errors import CredentialsError, FlowBlocked, SchedulerError, flow_block_reason
from core.event_bus import Event, MessageReceived
from core.plugin_base import PluginBase, PluginContext
from core.priority import Priority
from core.settings_manager import Setting

from .textutils import DEFAULT_PREFIX, build_exporter_chunks


class ExporterPlugin(PluginBase):
    """Export primary bot's soulmate list as chunks ready for '$exporter' import."""

    name = "exporter"
    version = "2.0.0"
    description = "Export your the_bot soulmate list and re-import it elsewhere."

    def __init__(self) -> None:
        # K16 (S4-TC1): one copy flow per (account, plugin) instance at a
        # time — concurrent triggers would interleave flows and double-send.
        self._in_flight = False

    settings = (
        Setting("trigger", "str", "export", "Command word that starts the copy (owners only)."),
        Setting(
            "dm_read_timeout",
            "float",
            10.0,
            "Idle seconds before DM reading stops.",
            min_value=0.5,
            max_value=120.0,
        ),
        Setting(
            "inter_chunk_delay",
            "float",
            10.0,
            "Seconds between sending chunks.",
            min_value=0.0,
            max_value=120.0,
        ),
        Setting(
            "max_chunk_length",
            "int",
            2000,
            "Hard Discord message limit.",
            min_value=50,
            max_value=2000,
        ),
        Setting("export_command", "str", "$dump", "the_bot command that DMs the soulmate list."),
        Setting("import_prefix", "str", DEFAULT_PREFIX, "Prefix used for the produced chunks."),
    )

    # -------------------------------------------------------------- lifecycle
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.logger.info("exporter loaded (trigger=%r)", ctx.settings["trigger"])

    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        """React to the trigger message; ignore everything else fast."""
        if not isinstance(event, MessageReceived):
            return
        if (event.content or "").strip() != ctx.settings["trigger"]:
            return
        if event.author_id not in ctx.services.credentials.owner_user_ids:
            ctx.logger.warning(
                "ignoring trigger from non-owner %s on account %s (if this is you, "
                "add the id to OWNER_USER_IDS in .env)",
                event.author_id,
                ctx.account,
            )
            return
        if self._in_flight:
            # K16: overlapping flows cross-collect each other's pages (each
            # subscription matches BOTH exports) — drop the re-trigger
            ctx.logger.info("copy flow already running on %s — trigger ignored", ctx.account)
            return
        self._in_flight = True
        try:
            await self._run_copy(ctx, event.channel_id)
        except FlowBlocked as exc:
            # Flow safety contract: one clean line for THIS account, then stop
            # — no partial chunks, no re-raise (the dispatcher would only see
            # a crash; the flow itself is simply over). See module docstring.
            ctx.logger.warning("flow aborted for %s: %s", ctx.account, flow_block_reason(exc))
        except (ValueError, CredentialsError, SchedulerError) as exc:
            # same clean-abort contract for the non-FlowBlocked failures the
            # flow can actually hit: bad config (prefix > budget), missing
            # PRIMARY_BOT_ID, cancelled/timed-out sends during a stop/estop
            ctx.logger.warning("flow aborted for %s: %s", ctx.account, exc)
        finally:
            self._in_flight = False  # release the K16 single-flow latch

    # ------------------------------------------------------------------- core
    async def _run_copy(self, ctx: PluginContext, trigger_channel: int) -> None:
        """Drive the full copy flow for one trigger.

        Raises FlowBlocked (TargetGone/AccessDenied) when the target channel
        is unusable — handle_event renders the abort log line from it.
        """
        # cache-aware: validate BEFORE acting so a gone channel aborts the
        # flow before a single message leaves the account
        ctx.services.validate_channel(trigger_channel)
        bot_id = ctx.services.credentials.primary_bot_id
        dm_channel = await ctx.services.open_dm(bot_id)

        async def _request_export() -> None:
            await ctx.services.send_message(
                trigger_channel, ctx.settings["export_command"], priority=Priority.NORMAL
            )

        # ask primary bot for the list (in the trigger channel), reading the DM
        # replies as they arrive — same gather structure as the original
        ctx.logger.info("requesting soulmate export from the_bot")
        collect = asyncio.ensure_future(self._collect_pages(ctx, bot_id, dm_channel))
        try:
            pages, _ = await asyncio.gather(collect, _request_export())
        except BaseException:
            # a failed export must not leave the collector subscribed for
            # the rest of the idle window (it would cross-collect the pages
            # of the NEXT flow — the K16 single-flow invariant)
            collect.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await collect
            raise
        ctx.logger.info("collected %d soulmate page(s)", len(pages))

        chunks = build_exporter_chunks(
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

    async def _collect_pages(
        self, ctx: PluginContext, bot_id: int, dm_channel: int
    ) -> list[MessageReceived]:
        """Collect primary bot DM pages until the idle timeout expires."""
        return await ctx.services.collect_messages(
            check=lambda e: e.author_id == bot_id and e.channel_id == dm_channel,
            idle_timeout=ctx.settings["dm_read_timeout"],
        )


__all__ = ["ExporterPlugin", "flow_block_reason"]
