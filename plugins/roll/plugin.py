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
from typing import Any, Dict, List, Optional

from core.errors import FlowBlocked, PluginError, SettingsValidationError
from core.event_bus import Event, MessageReceived
from core.plugin_base import (
    CommandContext,
    CommandSpec,
    PluginBase,
    PluginContext,
)
from core.plugin_scopes import channel_allowed
from core.priority import Priority
from core.settings_manager import Setting

#: roulette commands Mudae accepts
VALID_ROULETTES = ("wa", "wg", "wx", "w", "ha", "hg", "hx", "h",
                   "ma", "mg", "mx", "m")

#: transient-retry backoff bounds (seconds): starts at 5, doubles up to 60
TRANSIENT_BACKOFF_START = 5.0
TRANSIENT_BACKOFF_MAX = 60.0

#: error-text markers of a transient transport/pacing failure (fallback for
#: errors that arrive untyped — see :func:`is_transient_failure`)
_TRANSIENT_MARKERS = ("not running", "connecting", "reconnecting",
                      "did not complete within")


def is_transient_failure(exc: BaseException) -> bool:
    """True when ``exc`` looks like a transient transport/pacing failure.

    PRIMARY signal: a typed ``ConnectionError``/``TimeoutError`` — the
    services layer reconstructs those across the task-ack seam once the
    scheduler's internal retries are exhausted. FALLBACK (kept for errors
    that still arrive untyped, e.g. SchedulerError text from the ack
    timeout): substring match on the error message. The matching lives
    ONLY here so the typed path can eventually replace it wholesale.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    text = str(exc)
    return any(marker in text for marker in _TRANSIENT_MARKERS)


class RouletteCommandSetting(Setting):
    """``command`` setting that only accepts a Mudae roulette command.

    Values are normalized to the canonical ``$<roulette>`` form: ``wa``,
    ``$WA`` and `` $wa `` all become ``$wa``. Anything else is rejected
    with an error listing every valid roulette command (round-3: the
    constant existed but nothing enforced it).
    """

    def validate(self, value: Any) -> str:
        coerced = super().validate(value)
        cmd = str(coerced).strip().lower()
        if cmd.startswith("$"):
            cmd = cmd[1:]
        if cmd not in VALID_ROULETTES:
            raise SettingsValidationError(
                self.name,
                f"{coerced!r} is not a Mudae roulette command (valid: "
                + ", ".join(f"${r}" for r in VALID_ROULETTES) + ")",
            )
        return f"${cmd}"

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
        RouletteCommandSetting("command", "str", "$wa",
                               "Roulette command: $wa/$wg/$wx/$w/$ha/$hg/$hx/$h/$ma/$mg/"
                               "$mx/$m (validated; anything else is rejected)."),
        Setting("userstack", "bool", True,
                "Send '$us 20' when rolls run out (auto-disables if the "
                "account has no kakeraloots; an explicit re-enable via the "
                "settings survives restarts)."),
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
        #: the live PluginContext of the current activation — command
        #: handlers reuse it to drive the SAME internals (roll loop,
        #: settings view) the trigger-word path uses
        self._ctx: Optional[PluginContext] = None

    # ------------------------------------------------------------ lifecycle
    async def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        # restore persisted userstack availability (disabled when the
        # account ran out of kakeraloots in a previous session).
        #
        # ONE-WAY-LATCH FIX (round-3): the stored False only forces the
        # setting OFF when the user has NOT set 'userstack' explicitly.
        # The auto-disable flips the LIVE dict only — it never persists a
        # setting — so at load time an un-set key resolves to the schema
        # default (True), indistinguishable BY VALUE from an explicit
        # re-enable. ctx.explicit_settings (keys present in any settings
        # layer, built by the plugin manager) carries the distinction:
        # an explicit choice — re-enable OR disable — always wins.
        available = await ctx.services.state_get("userstack_available", True)
        if available is False:
            explicit = ctx.explicit_settings or frozenset()
            if "userstack" in explicit:
                ctx.logger.info(
                    "userstack was auto-disabled last session but is "
                    "explicitly set — honoring the explicit value")
            else:
                ctx.settings["userstack"] = False
        ctx.logger.info("roll v%s loaded (command=%r userstack=%s)",
                        self.version, ctx.settings["command"],
                        ctx.settings["userstack"])

    async def on_enable(self, ctx: PluginContext) -> None:
        # every (re)activation hands the plugin its live context — command
        # handlers stash-restore it so verbs always drive fresh services
        self._ctx = ctx

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
        text = (event.content or "").strip()

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

    async def _sleep_while_rolling(self, ctx: PluginContext, account: str,
                                   duration: float) -> None:
        """Sleep up to ``duration`` in chunks so a stop() lands quickly."""
        remaining = float(duration)
        while remaining > 0 and self._rolling.get(account):
            chunk = min(remaining, 30.0)
            await asyncio.sleep(chunk)
            remaining -= chunk

    async def _roll_loop(self, ctx: PluginContext, account: str,
                         channel_id: int) -> None:
        """The rolling loop — one iteration per roulette send.

        Each iteration: check cooldown → send the command → read the
        response → parse for cooldown/userstack/key-limit → act → sleep.
        Cancellation-safe: the stop trigger or on_disable cancels this task.
        """
        mudae_id = ctx.services.credentials.mudae_bot_id
        retry_backoff = TRANSIENT_BACKOFF_START
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
                    retry_backoff = TRANSIENT_BACKOFF_START  # healthy: reset
                except asyncio.CancelledError:
                    raise  # stop trigger / shutdown — outer handler logs it
                except PluginError as exc:
                    # framework services (cooldowns/state) not wired: this
                    # roll loop cannot work at all — name it precisely
                    # instead of landing in the generic crash line.
                    ctx.logger.error(
                        "plugin services unavailable on %s (%s) — roll loop "
                        "stopped; check the framework wiring", account, exc)
                    break
                except FlowBlocked as exc:
                    # one clean line, then the flow (and loop) is over
                    ctx.logger.warning("flow aborted for %s: %s",
                                        account, exc.reason)
                    break
                except Exception as exc:
                    # ConnectionError during a reconnect is TRANSIENT — the
                    # supervisor will bring the account back; back off and
                    # RETRY the send instead of dying.
                    error_text = str(exc)
                    if "scheduler is stopped" in error_text:
                        # K11 (S5-C4): global estop/shutdown stops the
                        # scheduler before plugins are deactivated. The
                        # loop is being torn down with the account; exit
                        # quietly instead of logging a misleading crash.
                        ctx.logger.info(
                            "roll loop stopped with scheduler for %s", account)
                        break
                    if is_transient_failure(exc):
                        # HONEST RECOVERY (round-3): there is no cheap probe
                        # that proves the account is back — the previous
                        # validate_channel probe was a pure cache lookup
                        # that ALWAYS passed, logging "recovered" while the
                        # account was still down and spinning every 5s.
                        # The retry send itself is the probe: wait a
                        # GROWING backoff (bounded start→max) and let the
                        # next iteration succeed or fail on its own merits.
                        ctx.logger.warning(
                            "send failed on %s (%s: %s) — account still "
                            "down, retrying in %.0fs (backoff grows to %.0fs)",
                            account, type(exc).__name__, exc,
                            retry_backoff, TRANSIENT_BACKOFF_MAX)
                        await self._sleep_while_rolling(ctx, account,
                                                        retry_backoff)
                        retry_backoff = min(retry_backoff * 2.0,
                                            TRANSIENT_BACKOFF_MAX)
                        continue
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
            if minutes is not None:
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
        embeds = getattr(rich, "embeds", None) or ()
        if not embeds:
            return

        try:
            embed = embeds[0]
        except (TypeError, IndexError):
            return
        footer_text = getattr(embed, "footer_text", None) or ""
        description = getattr(embed, "description", None) or ""

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

    # -------------------------------------------------------- self-commands
    #: the Mudae-style verb set (<plugin><verb>). Handlers are the plain
    #: methods below — the plugin manager binds them to this per-account
    #: instance, so they drive the SAME internals (roll loop, live
    #: settings) as the trigger-word path. Replies flow through the paced
    #: pipeline via the executor; ValueError renders the usage line.
    #: (The tuple sits AFTER the handler definitions: a class body is a
    #: sequential namespace — bare names must exist by then.)

    @staticmethod
    def _scope_summary(scope: Optional[Dict[str, Any]]) -> str:
        """One-line channel-scope description for replies."""
        if not scope or not isinstance(scope, dict):
            return "all channels"
        mode = scope.get("mode") or "all"
        if mode == "all":
            return "all channels"
        rules = [r for r in (scope.get("rules") or []) if isinstance(r, dict)]
        channels = sum(len(r.get("channel_ids") or []) for r in rules)
        return f"{mode}-list, {channels} channel(s) in {len(rules)} guild(s)"

    async def _cmd_rollstart(self, ctx: CommandContext,
                             args: List[str]) -> str:
        """Start the roll loop — subsumes the old ``sm!roll`` trigger.

        Target channel = the invoking channel, or an explicit id. The
        effective channel scope (the bound profile's reserved ``channels``
        key) is checked FIRST: a forbidden target gets an explaining
        refusal, never a silent no-op.
        """
        target = ctx.channel_id
        if args:
            try:
                target = int(args[0])
            except (TypeError, ValueError):
                raise ValueError(
                    f"channel id must be a number — rollstart [channel_id]"
                ) from None
        try:
            info = ctx.services.validate_channel(target)
        except FlowBlocked as exc:
            reason = getattr(exc, "reason", None) or str(exc)
            raise ValueError(f"channel {target} is unusable: {reason}") from None
        guild_id = getattr(info, "guild_id", None)
        if guild_id is None and target == ctx.channel_id:
            guild_id = ctx.guild_id
        scope: Optional[Dict[str, Any]] = None
        if ctx.settings_api is not None:
            try:
                scope = (await ctx.settings_api.effective()).get("channels")
            except Exception:
                ctx.logger.debug("effective settings unavailable", exc_info=True)
        if scope and not channel_allowed(scope, guild_id, target):
            return (f"refused: channel {target} is outside the roll channel "
                    f"scope ({self._scope_summary(scope)}) — the bound "
                    "profile's channels decide where roll may act")
        if self._rolling.get(ctx.account):
            return (f"already rolling on {ctx.account} — use rollstop first")
        plugin_ctx = self._ctx
        if plugin_ctx is None:
            raise ValueError("roll is not active on this account "
                             "(re-enable the plugin and retry)")
        self._start(plugin_ctx, ctx.account, target)
        label = ctx.services.describe_channel(target) or str(target)
        return (f"rolling started in {label} "
                f"({plugin_ctx.settings['command']})")

    async def _cmd_rollstop(self, ctx: CommandContext,
                            args: List[str]) -> str:
        """Stop the roll loop (same path as the ``rollstop`` trigger)."""
        was_rolling = bool(self._rolling.get(ctx.account))
        if self._ctx is not None:
            await self._stop(self._ctx, ctx.account)
        else:  # never activated here: just clear the bookkeeping
            self._rolling[ctx.account] = False
            task = self._tasks.pop(ctx.account, None)
            if task is not None and not task.done():
                task.cancel()
        return "rolling stopped" if was_rolling else "not rolling"

    async def _cmd_rollinfo(self, ctx: CommandContext,
                            args: List[str]) -> str:
        """Effective settings + profile binding + scope summary."""
        if ctx.settings_api is not None:
            effective = await ctx.settings_api.effective()
            profile = await ctx.settings_api.binding()
            scope = effective.pop("channels", None)
            source = "merged (defaults <- profile <- account overrides)"
        else:  # stripped stack: the live view only
            effective = dict(ctx.services.settings or {})
            profile = None
            scope = None
            source = "live view (profile storage unavailable)"
        lines = [f"roll on {ctx.account} — {source}",
                 f"profile: {profile if profile else 'none'}"]
        for key in sorted(effective):
            lines.append(f"  {key} = {effective[key]!r}")
        lines.append(f"scope: {self._scope_summary(scope)}")
        lines.append(f"rolling: {'yes' if self._rolling.get(ctx.account) else 'no'}")
        return "\n".join(lines)

    async def _cmd_rolllist(self, ctx: CommandContext,
                            args: List[str]) -> str:
        """The plugin's settings profiles (reference for binding)."""
        if ctx.settings_api is None:
            return "profile storage is unavailable on this stack"
        profiles = await ctx.settings_api.profiles()
        if not profiles:
            return "no roll profiles defined (create one in the dashboard)"
        return "roll profiles:\n" + "\n".join(
            f"  {row['name']} — {row['description'] or '-'}"
            for row in profiles)

    async def _cmd_rollset(self, ctx: CommandContext,
                           args: List[str]) -> str:
        """Set one per-account override — the SAME validated path as the
        dashboard's set-setting surface (bad keys/values render usage)."""
        if len(args) < 2:
            raise ValueError("rollset <key> <value> — e.g. rollset command $wg")
        if ctx.settings_api is None:
            return "settings storage is unavailable on this stack"
        key, value = args[0], " ".join(args[1:])
        effective = await ctx.settings_api.effective()
        old = effective.get(key)
        new = await ctx.settings_api.set(key, value)  # SettingsValidationError
        return f"set roll.{key} = {new!r} (was {old!r})"

    async def _cmd_rollremove(self, ctx: CommandContext,
                              args: List[str]) -> str:
        """Clear one per-account override (falls back to profile/default)."""
        if not args:
            raise ValueError("rollremove <key>")
        if ctx.settings_api is None:
            return "settings storage is unavailable on this stack"
        key = args[0]
        effective = await ctx.settings_api.clear(key)
        return f"cleared roll.{key} override — now {effective!r}"

    #: declared LAST: every handler name above already exists in the
    #: class namespace (a class body is sequential)
    COMMANDS = (
        CommandSpec(
            name="rollinfo",
            help="Show effective roll settings, profile binding and scope",
            handler=_cmd_rollinfo,
        ),
        CommandSpec(
            name="rolllist",
            help="List the roll plugin's settings profiles",
            handler=_cmd_rolllist,
        ),
        CommandSpec(
            name="rollremove",
            help="Clear one per-account roll setting override",
            usage="<key>",
            handler=_cmd_rollremove,
        ),
        CommandSpec(
            name="rollset",
            help="Set one per-account roll setting override",
            usage="<key> <value>",
            handler=_cmd_rollset,
        ),
        CommandSpec(
            name="rollstart",
            help="Start rolling (current or given channel, scope-checked)",
            usage="[channel_id]",
            handler=_cmd_rollstart,
        ),
        CommandSpec(
            name="rollstop",
            help="Stop rolling",
            handler=_cmd_rollstop,
        ),
    )


__all__ = ["RollPlugin", "parse_pause_minutes", "VALID_ROULETTES",
           "is_transient_failure", "RouletteCommandSetting"]
