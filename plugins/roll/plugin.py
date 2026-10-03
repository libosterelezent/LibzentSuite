"""Roll plugin: Mudae roulette automation with cooldowns, userstack and key limits.

Ported from MudaeScript/cogs/roll_profile.py onto the SlaughterMudae
plugin architecture. The Discord command interface (rollset/rollstart/
rollstop etc.) is replaced by the framework's trigger-word model and
persistent settings — the ROLLING LOGIC is preserved exactly:

  * sends the configured roulette command ($wa/$wg/$m/...) in a loop,
  * parses Mudae's responses (plain text + embeds) for:
      - "roulette is limited to **N** min" -> persistent cooldown,
      - "2 ROLLS LEFT" in the embed footer -> $us 20 (userstack),
      - key-per-hour limit in the embed description -> $ru + cooldown,
  * userstack can be auto-disabled when the account runs out,
  * cooldowns survive restarts via the framework's CooldownService
    (absolute expiry timestamps, not relative timers),
  * every send flows through the paced pipeline (per-task + group +
    lane + floor + jitter) — the plugin NEVER self-throttles.

The plugin uses the rich event model (DiscordMessage.embeds) to parse
Mudae's embed responses, and PluginStateService to persist runtime
state (userstack availability) across restarts.
"""
from __future__ import annotations

import asyncio
import re
from typing import Dict, Optional

from core.errors import FlowBlocked
from core.event_bus import Event, MessageReceived
from core.plugin_base import PluginBase, PluginContext
from core.priority import Priority
from core.settings_manager import Setting

#: roulette commands Mudae accepts
VALID_ROULETTES = ("wa", "wg", "wx", "w", "ha", "hg", "hx", "h",
                   "ma", "mg", "mx", "m")

#: cooldown key used with the framework's CooldownService
COOLDOWN_KEY = "roulette"

#: regex for Mudae's "**N** min" cooldown messages
_PAUSE_TIME_RE = re.compile(r"\*\*(\d+)\*\*\s*min\s*", re.IGNORECASE)

#: Mudae messages that indicate userstack is unavailable
_USERSTACK_FAIL = ("You need to buy kakeraloots",
                   "You don't have enough rolls stacked!")

#: embed footer text indicating the account is running low on rolls
_LOW_ROLLS_MARKER = "2 ROLLS LEFT"

#: embed description line indicating the per-hour key limit
_KEY_LIMIT_MARKER = "(You reached the limit of 1000 keys per hour!)"


def parse_pause_minutes(text: str) -> Optional[int]:
    """Extract '**N** min' from a Mudae message; None when absent."""
    match = _PAUSE_TIME_RE.search(text or "")
    return int(match.group(1)) if match else None


class RollPlugin(PluginBase):
    """Continuous Mudae roulette with cooldown detection and recovery."""

    name = "roll"
    version = "2.0.0"
    description = (
        "Mudae roulette automation: sends the configured command in a loop, "
        "parses cooldown/userstack/key-limit responses, and persists "
        "cooldowns across restarts."
    )

    settings = (
        Setting("trigger", "str", "roll",
                "Word that starts rolling in the channel it's typed in (owners only)."),
        Setting("stop_trigger", "str", "rollstop",
                "Word that stops rolling (owners only)."),
        Setting("command", "str", "$wa",
                "Roulette command: $wa/$wg/$wx/$w/$ha/$hg/$hx/$h/$ma/$mg/$mx/$m."),
        Setting("userstack", "bool", True,
                "Send '$us 20' when rolls run out (auto-disables if the "
                "account has no kakeraloots)."),
        Setting("ignore_keys", "bool", False,
                "Skip '$ru' when the per-hour key limit is reached."),
        Setting("roll_interval", "float", 2.0,
                "Minimum seconds between roulette sends (the pipeline's "
                "rate limiter adds its own pacing on top).",
                min_value=1.5, max_value=120.0),
        Setting("response_timeout", "float", 8.0,
                "Seconds to wait for Mudae's response before assuming "
                "the roll went through.", min_value=2.0, max_value=30.0),
    )

    def __init__(self) -> None:
        self._rolling: Dict[str, bool] = {}   # account -> is currently rolling
        self._tasks: Dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------ lifecycle
    async def on_load(self, ctx: PluginContext) -> None:
        # restore persisted userstack availability (disabled when the
        # account ran out of kakeraloots in a previous session)
        available = await ctx.services.state_get("userstack_available", True)
        if available is False:
            ctx.settings["userstack"] = False
        ctx.logger.info("roll v%s loaded (command=%r userstack=%s)",
                        self.version, ctx.settings["command"],
                        ctx.settings["userstack"])

    async def on_disable(self, ctx: PluginContext) -> None:
        await self._stop_all(ctx)

    async def on_unload(self, ctx: PluginContext) -> None:
        await self._stop_all(ctx)

    # ------------------------------------------------------------ events
    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        if not isinstance(event, MessageReceived):
            return
        if event.author_id not in ctx.services.credentials.owner_user_ids:
            return
        text = event.content.strip()

        if text == ctx.settings["stop_trigger"]:
            await self._stop(ctx, event.account)
            return
        if text != ctx.settings["trigger"]:
            return

        if self._rolling.get(event.account):
            ctx.logger.info("already rolling on %s — ignored", event.account)
            return

        # validate BEFORE acting (flow-abort contract)
        try:
            ctx.services.validate_channel(event.channel_id)
        except FlowBlocked as exc:
            ctx.logger.warning("flow aborted for %s: %s", ctx.account, exc.reason)
            return
        self._start(ctx, event.account, event.channel_id)

    # ------------------------------------------------------------ rolling
    def _start(self, ctx: PluginContext, account: str, channel_id: int) -> None:
        self._rolling[account] = True
        task = asyncio.get_running_loop().create_task(
            self._roll_loop(ctx, account, channel_id))
        self._tasks[account] = task
        ctx.logger.info("rolling started on %s in channel %s",
                        account, channel_id)

    async def _stop(self, ctx: PluginContext, account: str) -> None:
        self._rolling[account] = False
        task = self._tasks.pop(account, None)
        if task is not None and not task.done():
            task.cancel()
        ctx.logger.info("rolling stopped on %s", account)

    async def _stop_all(self, ctx: PluginContext) -> None:
        for account in list(self._rolling):
            await self._stop(ctx, account)

    async def _roll_loop(self, ctx: PluginContext, account: str,
                         channel_id: int) -> None:
        """The rolling loop — one iteration per roulette send.

        Each iteration: check cooldown → send the command → read the
        response → parse for cooldown/userstack/key-limit → act → sleep.
        Cancellation-safe: the stop trigger or on_disable cancels this task.
        """
        mudae_id = ctx.services.credentials.mudae_bot_id
        try:
            while self._rolling.get(account):
                # ── ONE iteration, with per-iteration error handling ──
                # (isolation audit K11, follow-up: the transient branch
                # previously lived in an except AROUND this whole loop —
                # after "recovered — resuming" it fell into finally and
                # the loop DIED anyway. Handling per iteration + continue
                # makes the documented reconnection resilience real.)
                try:
                    # ── cooldown check (persistent, absolute expiry) ──
                    remaining = await ctx.services.cooldown_remaining(COOLDOWN_KEY)
                    if remaining > 0:
                        ctx.logger.info("cooldown active: %.0fs remaining "
                                        "(sleeping in 30s chunks)", remaining)
                        # sleep in chunks so a stop() takes effect quickly
                        while remaining > 0 and self._rolling.get(account):
                            chunk = min(remaining, 30.0)
                            await asyncio.sleep(chunk)
                            remaining -= chunk
                        continue

                    # ── send the roulette command ──
                    ctx.services.validate_channel(channel_id)
                    ctx.logger.debug("sending %s", ctx.settings["command"])
                    await ctx.services.send_message(
                        channel_id, ctx.settings["command"],
                        priority=Priority.NORMAL,
                    )

                    # ── read Mudae's response ──
                    response = await ctx.services.wait_for_message(
                        check=lambda e: (e.author_id == mudae_id
                                         and e.channel_id == channel_id),
                        timeout=ctx.settings["response_timeout"],
                    )
                    if response is None:
                        ctx.logger.debug("no response from Mudae — retrying")
                        await asyncio.sleep(ctx.settings["roll_interval"])
                        continue

                    # ── parse the response for cooldowns/limits ──
                    await self._parse_response(ctx, account, channel_id,
                                               response, mudae_id)

                    await asyncio.sleep(ctx.settings["roll_interval"])
                except asyncio.CancelledError:
                    raise  # stop trigger / shutdown — outer handler logs it
                except FlowBlocked as exc:
                    # one clean line, then the flow (and loop) is over
                    ctx.logger.warning("flow aborted for %s: %s",
                                        account, exc.reason)
                    break
                except Exception as exc:
                    # ConnectionError during a reconnect is TRANSIENT — the
                    # supervisor will bring the account back; wait, probe,
                    # and CONTINUE the loop instead of dying.
                    error_text = str(exc)
                    if "scheduler is stopped" in error_text:
                        # K11 (S5-C4): global estop/shutdown stops the
                        # scheduler before plugins are deactivated. The
                        # loop is being torn down with the account; exit
                        # quietly instead of logging a misleading crash.
                        ctx.logger.info(
                            "roll loop stopped with scheduler for %s", account)
                        break
                    if "not running" in error_text or "connecting" in error_text \
                            or "reconnecting" in error_text \
                            or "did not complete within" in error_text:
                        # "did not complete within" = the 30s task-ack
                        # timeout: the send sat in pacing/queue longer than
                        # the ack window (e.g. the fleet send lane paused) —
                        # transient, NOT a crash (K11 / S5-C2: previously
                        # this killed the loop for good).
                        ctx.logger.warning(
                            "account %s temporarily disconnected — roll loop "
                            "pausing until reconnection", account)
                        while self._rolling.get(account):
                            await asyncio.sleep(5.0)
                            # probe: can we reach the channel again?
                            try:
                                ctx.services.validate_channel(channel_id)
                                break  # account is back — resume rolling
                            except Exception:
                                continue
                        ctx.logger.info(
                            "account %s recovered — roll loop resuming",
                            account)
                        continue  # ACTUALLY resume the outer rolling loop
                    ctx.logger.exception("roll loop crashed on %s", account)
                    break
        except asyncio.CancelledError:
            ctx.logger.info("roll loop cancelled for %s", account)
        finally:
            self._rolling[account] = False
            self._tasks.pop(account, None)

    # ------------------------------------------------------------ parsing
    async def _parse_response(self, ctx: PluginContext, account: str,
                              channel_id: int, response: MessageReceived,
                              mudae_id: int) -> None:
        """Parse one Mudae response and react (cooldown/userstack/$ru).

        This is the core logic ported from the original roll_profile.py's
        message-history scan, adapted to our single-response event model.
        """
        content = response.content or ""

        # ── "the roulette is limited to **N** min" → cooldown ──
        if "the roulette is limited to" in content:
            minutes = parse_pause_minutes(content)
            if minutes:
                await ctx.services.cooldown_set(
                    COOLDOWN_KEY, minutes * 60,
                    source="mudae:roulette_limited",
                    reason=f"roulette limited for {minutes} min",
                )
                ctx.logger.info("cooldown set: %d min (persistent)", minutes)
                return

        # ── "the roulette is limited" (no specific time) + userstack ──
        if "the roulette is limited" in content and ctx.settings["userstack"]:
            await self._try_userstack(ctx, channel_id, mudae_id)
            return

        # ── embed responses (character rolls) ──
        rich = getattr(response, "message", None)
        if rich is None or not rich.embeds:
            return

        embed = rich.embeds[0]
        footer_text = embed.footer_text or ""
        description = embed.description or ""

        # ── "2 ROLLS LEFT" in footer + userstack → $us 20 ──
        if _LOW_ROLLS_MARKER in footer_text and ctx.settings["userstack"]:
            ctx.logger.info("low rolls detected — sending $us 20")
            await self._try_userstack(ctx, channel_id, mudae_id)

        # ── key limit reached → $ru + cooldown ──
        if (not ctx.settings["ignore_keys"]
                and _KEY_LIMIT_MARKER in description):
            ctx.logger.info("key limit reached — sending $ru")
            await ctx.services.send_message(
                channel_id, "$ru", priority=Priority.NORMAL)
            ru_reply = await ctx.services.wait_for_message(
                check=lambda e: (e.author_id == mudae_id
                                 and e.channel_id == channel_id
                                 and (e.content or "").startswith("You have")),
                timeout=5.0,
            )
            if ru_reply:
                minutes = parse_pause_minutes(ru_reply.content or "")
                if minutes:
                    await ctx.services.cooldown_set(
                        COOLDOWN_KEY, minutes * 60,
                        source="mudae:key_reset",
                        reason=f"key reset in {minutes} min",
                    )
                    ctx.logger.info("cooldown set from $ru: %d min", minutes)

    async def _try_userstack(self, ctx: PluginContext, channel_id: int,
                             mudae_id: int) -> None:
        """Send '$us 20'; on Mudae's refusal disable userstack NOW.

        The disable flips BOTH the persisted state (survives restarts via
        on_load) and the live settings view — without the live flip every
        later low-rolls response would repeat the doomed '$us 20' send
        until the next restart.
        """
        await ctx.services.send_message(
            channel_id, "$us 20", priority=Priority.NORMAL)
        us_reply = await ctx.services.wait_for_message(
            check=lambda e: (e.author_id == mudae_id
                             and e.channel_id == channel_id),
            timeout=5.0,
        )
        if us_reply and any(msg in (us_reply.content or "")
                            for msg in _USERSTACK_FAIL):
            ctx.logger.info("userstack unavailable — disabling")
            ctx.settings["userstack"] = False
            await ctx.services.state_set("userstack_available", False)


__all__ = ["RollPlugin", "parse_pause_minutes", "VALID_ROULETTES"]
