"""Remote control: manage the framework FROM Discord via mention prefixes.

An owner types ``@<selfbot account> <command>`` in any channel the account
can see; this processor picks it up, runs the command through the ONE
CoreAPI (same path as CLI/dashboard — parity by construction) and replies in
the same channel through the FULL send pipeline (scheduler -> lane ->
pacing -> jitter -> audit), so management traffic is rate-safe by design.

Safety model:
  * OWNER_USER_IDS only — anyone else's mention is ignored completely
    (no reply, no log noise beyond a debug line),
  * an explicit allowlist of read-mostly + safe commands; nothing here can
    change secrets or talk to Discord beyond the reply itself,
  * replies are truncated, self-throttled per account (reply_cooldown_s),
    and submitted at NORMAL priority through the paced pipeline,
  * opt-out: set ``REMOTE_CONTROL=0`` in .env to disable wiring entirely.

Mention syntax: the message must START with the account's own mention —
``<@user_id>`` or ``<@!user_id>`` (the bang form older clients emit) —
followed by whitespace and the command line. Each account answers only to
ITS OWN mention, so ``@alpha status`` and ``@beta status`` are independent.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from .credentials import Credentials
from .errors import (
    AccountError,
    CredentialsError,
    PluginError,
    ProfileError,
    RatePolicyError,
    SettingsValidationError,
)
from .event_bus import Event, EventBus, MessageReceived
from .priority import Priority

#: domain errors render their (safe, human-written) message bare; anything
#: else is an internal failure and must not leak into a Discord channel
_DOMAIN_ERRORS = (
    AccountError, CredentialsError, PluginError, ProfileError,
    RatePolicyError, SettingsValidationError, ValueError, RuntimeError,
)

LOG = logging.getLogger("slaughtermudae.remote_control")

#: hard cap on one reply (Discord's own limit is 2000 — headroom for safety)
MAX_REPLY_CHARS = 1900


class RemoteControl:
    """Mention-prefix command bridge: Discord messages -> ControlAPI -> reply."""

    def __init__(
        self,
        bus: EventBus,
        scheduler: Any,
        accounts: Any,
        api_provider: Callable[[], Any],
        credentials: Credentials,
        logger: Any = None,
        *,
        reply_cooldown_s: float = 1.0,
    ) -> None:
        """``api_provider`` is a zero-arg callable returning the ControlAPI
        (late-bound: bootstrap builds the API after the managers)."""
        self._bus = bus
        self._scheduler = scheduler
        self._accounts = accounts
        self._api_provider = api_provider
        self._credentials = credentials
        self._logger = logger or LOG
        self._cooldown_s = max(0.0, float(reply_cooldown_s))
        self._last_reply_at: Dict[str, float] = {}
        self._unsub: Optional[Callable[[], None]] = None
        self._pending: set = set()  # strong refs for fire-and-forget tasks
        self._handlers: Dict[str, Callable[[Any, str], Any]] = {}
        self._register_commands()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "RemoteControl":
        """Subscribe to inbound messages (idempotent)."""
        if self._unsub is None:
            self._unsub = self._bus.subscribe(MessageReceived, self._on_message)
            self._logger.info("remote control armed (mention prefix, owners only)")
        return self

    def stop(self) -> None:
        """Unsubscribe; in-flight handlers finish, no new ones start."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    # -------------------------------------------------------------- inbound
    def _on_message(self, event: Event) -> None:
        """Bus handler (sync): filter hard, then run the command as a task."""
        if not isinstance(event, MessageReceived):
            return
        line = self._extract_command(event)
        if line is None:
            return
        if event.author_id not in self._credentials.owner_user_ids:
            return  # non-owner mentions are simply ignored (no reply at all)
        task = asyncio.get_running_loop().create_task(self._handle(event, line))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _extract_command(self, event: MessageReceived) -> Optional[str]:
        """'<@uid> status accounts' -> 'status accounts' when the mention is
        THIS account's own. Returns '' for a BARE mention (owner invoked us
        without a command -> help), None when the message is not ours."""
        account = self._accounts.get(event.account) if hasattr(
            self._accounts, "get") else None
        if account is None:
            return None
        user_id = getattr(account.runtime, "user_id", None)
        if not user_id:
            return None
        text = (event.content or "").strip()
        for prefix in (f"<@{user_id}>", f"<@!{user_id}>"):
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        return None

    async def _handle(self, event: MessageReceived, line: str) -> None:
        """Run one command and reply in-channel (fully error-contained)."""
        account = event.account
        now = time.monotonic()
        last = self._last_reply_at.get(account)
        # EXECUTION is never throttled (a safety command like `estop` must
        # always run); only the REPLY is self-throttled — one per window.
        # Stamped BEFORE executing: two mentions arriving inside the
        # execution window must not both slip past the check.
        throttled = last is not None and now - last < self._cooldown_s
        self._last_reply_at[account] = now
        try:
            reply = await self._execute(line)
        except Exception as exc:  # a broken command must never crash the bridge
            if isinstance(exc, _DOMAIN_ERRORS):
                reply = f"error: {exc}" if str(exc) else f"error: {type(exc).__name__}"
            else:
                # unexpected internals (paths, sql) do not belong in a
                # Discord channel — the traceback goes to the logs
                self._logger.exception("remote control command failed")
                reply = "error: internal (see logs)"
        if throttled:
            return  # executed; the reply is suppressed by the cooldown
        reply = (reply or "ok").strip()
        if len(reply) > MAX_REPLY_CHARS:
            reply = reply[: MAX_REPLY_CHARS - 20] + "\n… (truncated)"
        try:
            self._scheduler.submit(
                account, "control", "message",
                channel_id=int(event.channel_id), content=reply,
                priority=Priority.NORMAL, name=f"control-reply-{event.message_id}",
            )
        except Exception:
            # e.g. the global `estop` just stopped the scheduler: the reply
            # CANNOT be delivered in-band. Log at ERROR with the full text —
            # the owner's confirmation must at least survive in the logs.
            self._logger.error(
                "remote control reply undeliverable (scheduler stopped?) — "
                "reply was: %s", reply
            )

    # -------------------------------------------------------------- commands
    async def _execute(self, line: str) -> str:
        """Allowlisted dispatch to the ControlAPI; 'help' lists everything."""
        api = self._api_provider()
        parts = line.split()
        if not parts:
            return await self._help()
        cmd, args = parts[0].lower(), parts[1:]
        handler = self._handlers.get(cmd)
        if handler is None:
            return f"unknown command {cmd!r} — try 'help'"
        try:
            return await handler(api, args)
        except Exception as exc:
            return f"error: {type(exc).__name__}: {exc}"

    def _register_commands(self) -> None:
        """The allowlist: read-mostly + explicitly safe mutations only."""
        self._handlers = {
            "help": self._help,
            "status": self._cmd_status,
            "accounts": self._cmd_accounts,
            "plugins": self._cmd_plugins,
            "ratelimit": self._cmd_ratelimit,
            "lane": self._cmd_lane,
            "tasks": self._cmd_tasks,
            "estop": self._cmd_estop,
            "simulation": self._cmd_simulation,
        }

    async def _help(self, api: Any = None, args: list = ()) -> str:
        return (
            "commands: status · accounts list|info <n>|start <n>|stop <n> · "
            "plugins <account> [<plugin> on|off] · ratelimit show|warnings · "
            "lane [show] · tasks · estop [account] · simulation list · help"
        )

    async def _cmd_status(self, api: Any, args: list) -> str:
        if args:
            return "'status' takes no arguments"
        status = await api.status()
        tasks = status.get("tasks") or {}
        accounts = status.get("accounts") or []
        lines = [
            f"uptime {status.get('uptime_s', 0):.0f}s · tasks "
            + " ".join(f"{k}={v}" for k, v in tasks.items() if k != "avg_duration_s")
        ]
        for account in accounts:
            lines.append(
                f"· {account.get('name')}: {account.get('status')}"
                + (f" (lat {account.get('latency_ms'):.0f}ms)"
                   if account.get("latency_ms") is not None else "")
            )
        warnings = status.get("warnings") or []
        if warnings:
            lines.append(f"{len(warnings)} warning(s) — 'ratelimit warnings'")
        return "\n".join(lines)

    async def _cmd_accounts(self, api: Any, args: list) -> str:
        if not args or args[0] == "list":
            accounts = await api.accounts_list()
            if not accounts:
                return "no accounts registered"
            return "\n".join(
                f"· {a['name']}: {a.get('status')} profiles={a.get('profiles') or '-'}"
                for a in accounts
            )
        sub, rest = args[0], args[1:]
        if sub == "info" and len(rest) == 1:
            info = await api.accounts_info(rest[0])
            st = info.get("status") or {}
            rt = info.get("runtime") or {}
            latency = st.get("latency_ms")
            return (
                f"{info['name']} — {st.get('state')}\n"
                f"latency {latency if latency is not None else '-'}ms · "
                f"uptime {(st.get('uptime_s') or 0):.0f}s\n"
                f"cached: {rt.get('cached_guilds')}g {rt.get('cached_channels')}c "
                f"{rt.get('cached_dms')}d (client cache)"
            )
        if sub == "start" and len(rest) == 1:
            await api.accounts_start(rest[0])
            return f"started {rest[0]}"
        if sub == "stop" and len(rest) == 1:
            await api.accounts_stop(rest[0])
            return f"stopped {rest[0]}"
        return "usage: accounts list|info <n>|start <n>|stop <n>"

    async def _cmd_plugins(self, api: Any, args: list) -> str:
        if not args:
            return "usage: plugins <account> [<plugin> on|off]"
        account = args[0]
        if len(args) == 1:
            states = await api.plugins_state(account)
            return "\n".join(
                f"{'+' if s['enabled'] else '-'} {s['name']}"
                + (f" ({s['error_count']} err)" if s.get("error_count") else "")
                for s in states
            ) or "no plugins"
        plugin, flag = args[1], (args[2].lower() if len(args) > 2 else "")
        if flag not in ("on", "off"):
            return "usage: plugins <account> <plugin> on|off"
        await api.plugins_set_enabled(account, plugin, flag == "on")
        return f"{plugin} {'enabled' if flag == 'on' else 'disabled'} on {account}"

    async def _cmd_ratelimit(self, api: Any, args: list) -> str:
        sub = args[0].lower() if args else "show"
        if sub == "show":
            policies = await api.ratelimit_policies()
            if not policies:
                return "no stored policies — everything paces at the floor"
            return "\n".join(
                f"· {p['level']}{':' + p['owner'] if p['owner'] else ''} "
                f"{p['action']}: {p['interval']}s +{p['jitter'][0]}..{p['jitter'][1]}s"
                for p in policies
            )
        if sub == "warnings":
            warnings = await api.ratelimit_warnings()
            if not warnings:
                return "no safety warnings — configuration looks sane"
            return "\n".join(f"[{w['level']}] {w['code']}: {w['message']}" for w in warnings)
        return "usage: ratelimit show|warnings"

    async def _cmd_lane(self, api: Any, args: list) -> str:
        lane = await api.ratelimit_lane()
        if not lane.get("enabled"):
            return "send lane: off (per-account pacing only)"
        state = "PAUSED (all outbound holds)" if lane.get("paused") else "active"
        return (f"send lane: on ({state}) · spacing {lane.get('prefixrate')}s · "
                f"waiting {lane.get('waiting')} · grants {lane.get('grants')}")

    async def _cmd_tasks(self, api: Any, args: list) -> str:
        rows = await api.tasks_list()
        if not rows:
            return "no queued/running tasks"
        return "\n".join(
            f"· #{t['id']} {t.get('account')}/{t.get('plugin')} {t.get('action')}"
            f" [{t.get('status')}]"
            for t in rows[:10]
        ) + (f"\n(+{len(rows) - 10} more)" if len(rows) > 10 else "")

    async def _cmd_estop(self, api: Any, args: list) -> str:
        if len(args) == 1:
            out = await api.accounts_emergency_stop(args[0], surface="remote")
            self._logger.info(
                "account-scoped estop reply may be undeliverable (account "
                "%s is stopped) — reply was: emergency stop %s: %s task(s) "
                "cancelled", args[0], args[0], out.get("cancelled_tasks", 0))
            return f"emergency stop {args[0]}: {out.get('cancelled_tasks', 0)} task(s) cancelled"
        if args:
            return "usage: estop [account]"
        out = await api.emergency_stop_all()
        return (
            f"GLOBAL EMERGENCY STOP — {out.get('cancelled_tasks', 0)} task(s) "
            "cancelled, all accounts halted. Restart manually."
        )

    async def _cmd_simulation(self, api: Any, args: list) -> str:
        scenarios = await api.simulation_list()
        if not scenarios:
            return "no scenarios available"
        return "\n".join(f"· {s['name']}: {s.get('description', '')}" for s in scenarios)


__all__ = ["RemoteControl", "MAX_REPLY_CHARS"]
