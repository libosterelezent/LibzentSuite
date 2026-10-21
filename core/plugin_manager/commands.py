"""Self-command hub: the per-account command registry (Phase 3).

Connects three parties WITHOUT new wiring in the account manager or the
bootstrap (both outside this phase's ownership):

  * the :class:`~core.plugin_manager.PluginManager` (the registry SOURCE)
    publishes an account's command surface whenever assignments or
    enabled-flags change — the same paths that already re-attach plugins
    (``attach_account`` / ``set_enabled`` / ``detach_account``);
  * each account's live Discord client (the registry CONSUMER) subscribes
    on connect and rebuilds its ``ext.commands`` Bot registrations from
    the published spec list;
  * invocations flow back through :meth:`SelfCommandHub.invoke`, which
    delegates to the bound manager's ``execute_self_command`` — handlers
    run with plugin-grade pacing (replies go through the per-plugin
    services facade into the task pipeline, NEVER a raw client send).

FRAMEWORK COMMANDS: the retired RemoteControl mention verbs
(``status``/``accounts``/``plugins``/``ratelimit``/``lane``/``tasks``/
``estop``/``simulation``, category "Framework"), the admin* settings
verbs, ``pg`` and ``say`` (category "Utilities") and the dynamic
``help`` ("No Category") are declared HERE, not by plugins — matching
Mudae's layout. They are first-class :class:`CommandSpec`s, so the
registry treats them exactly like plugin-declared verbs (and the
``help`` listing is built dynamically from whatever the registry holds —
``snipewatch``-style extras a later plugin declares appear in the help
with zero framework changes). The management verbs reach the ControlAPI
through the hub's ``api_provider`` (bootstrap-registered): ONE command
layer, one dispatch path, one authorization gate — the separate
RemoteControl bus handler is retired, so mention-prefix commands no
longer race the Bot when prefix_mode=mention.

COLLISION POLICY (deterministic): framework names are reserved, then
plugin specs are collected in manifest order (plugin name sort); the
FIRST registration of a name wins and every later collision is dropped
with a logged warning naming both plugins.

The hub is a process-wide singleton (like the adapter's IdentifyGate):
one framework runs per process. Tests that build several managers bind
the last one (documented last-wins) — executor-level tests can call
``PluginManager.execute_self_command`` directly, bypassing the hub.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..errors import DOMAIN_ERRORS
from ..plugin_base import ArgSpec, CommandContext, CommandSpec, usage_line

LOG = logging.getLogger("libzent.selfcommands")

#: services-facade plugin label used for framework-owned commands (task
#: attribution: the audit rows read ``<account>/selfcmd``)
FRAMEWORK_PLUGIN = "selfcmd"

#: category headers, in the order Mudae uses: plugin categories (sorted,
#: from the specs) come first, then Settings, then Utilities, then No
#: Category.
UTILITIES_CATEGORY = "Utilities"
SETTINGS_CATEGORY = "Settings"
NO_CATEGORY = "No Category"

#: the retired RemoteControl mention verbs (status/accounts/plugins/
#: ratelimit/lane/tasks/estop/simulation) — framework management from
#: chat, ported into the ONE command layer.
FRAMEWORK_CATEGORY = "Framework"

#: domain errors render their (safe, human-written) message bare as an
#: ``error: …`` reply (the retired remote control's convention); anything
#: else is an internal failure that raises on — the executor masks it.
#: NOTE: bare RuntimeError is deliberately NOT domain — typed domain
#: errors that subclass RuntimeError (AccountError etc.) still render.
_DOMAIN_ERRORS = DOMAIN_ERRORS


class PacingBackend:
    """Bridge the ``prefixrate`` framework verb to the rate limiter.

    Registered ONCE by the bootstrap (which owns the limiter) —
    ``COMMAND_HUB.pacing = PacingBackend(limiter)``. Duck-typed so tests
    can stub it; when unregistered, prefixrate answers honestly that
    pacing control is unavailable on this stack.
    """

    def __init__(self, limiter: Any) -> None:
        self._limiter = limiter

    def get(self, account: str) -> Optional[float]:
        try:
            return float(self._limiter.effective_interval("message", account))
        except Exception:
            return None

    def set(self, account: str, seconds: float) -> Optional[float]:
        """Set the ACCOUNT's message interval; returns the previous
        effective interval (None on failure)."""
        try:
            previous = float(
                self._limiter.effective_interval("message", account))
            self._limiter.set_policy_values(
                "message", seconds, 0.1, 0.5,
                level="account", owner=account)
            return previous
        except Exception:
            return None

#: hard cap on one command reply (Discord's own limit is 2000; the same
#: headroom convention as core.remote_control)
MAX_REPLY_CHARS = 1900


# ------------------------------------------------- ported remote-control bridge
def _framework_api() -> Any:
    """The ControlAPI for the ported framework management verbs.

    Reaches the process hub's ``api_provider`` (registered by the
    bootstrap right after the ControlAPI is built — the same late-bound
    seam as ``pacing`` and ``access``). Raises ValueError with an honest
    message on stacks that never wired one (direct-construction tests).
    """
    provider = COMMAND_HUB.api_provider
    api = provider() if callable(provider) else provider
    if api is None:
        raise ValueError("framework management unavailable on this stack")
    return api


def _ported(command: Callable) -> Callable:
    """Decorator for the ported remote-control verbs: ControlAPI domain
    errors render their (safe) message bare — ``error: …`` — exactly like
    the retired mention bridge did; internal failures raise on and the
    executor masks them (``failed internally, see logs``)."""
    @functools.wraps(command)
    async def guarded(ctx: CommandContext, args: List[str]) -> str:
        try:
            return await command(ctx, args)
        except _DOMAIN_ERRORS as exc:
            return f"error: {exc}" if str(exc) else f"error: {type(exc).__name__}"
    return guarded


def _fmt_uptime(seconds: Optional[float]) -> str:
    """Human uptime: 1h23m / 4m05s / 12s ('-' when unknown)."""
    if seconds is None or seconds < 0:
        return "-"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# ------------------------------------------------------- framework handlers
async def _pg_handler(ctx: CommandContext, args: List[str]) -> str:
    """Gateway latency + uptime of THIS account (self-command layer)."""
    stats = COMMAND_HUB.runtime_stats(ctx.account)
    latency = stats.get("latency_ms")
    uptime = stats.get("uptime_s")
    latency_text = f"{latency:.0f}ms" if isinstance(latency, (int, float)) else "-"
    return (f"{ctx.account}: gateway {latency_text} · "
            f"uptime {_fmt_uptime(uptime)}")


async def _say_handler(ctx: CommandContext, args: List[str]) -> str:
    """Repeat the invoker's message verbatim (whitespace-normalized)."""
    if not args:
        raise ValueError("say needs something to repeat")
    return " ".join(args)


async def _help_handler(ctx: CommandContext, args: List[str]) -> str:
    """Plugin-grouped listing of every registered command (dynamic,
    paginated). ``help`` -> page 1, ``help <N>`` -> page N,
    ``help <command>`` -> usage + description."""
    specs = COMMAND_HUB.specs(ctx.account)
    if args and args[0].isdigit():            # help 2 -> page 2
        pages = _paginate_help(_render_help_body(specs))
        n = int(args[0])
        if not 1 <= n <= len(pages):
            raise ValueError(f"help has {len(pages)} page(s)")
        suffix = f"\n(help {n + 1} for more…)" if n < len(pages) else ""
        return pages[n - 1] + suffix
    if args:  # help <command>: usage + description
        wanted = args[0].lower()
        for spec in specs:
            if spec.name == wanted:
                lines = [f"{spec.name} — {spec.help}"]
                generated = spec.usage or usage_line(spec)
                lines.append(f"usage: {spec.name} {generated}".rstrip())
                lines.append(f"category: {spec.category or NO_CATEGORY}")
                return "\n".join(lines)
        known = " ".join(sorted(s.name for s in specs))
        raise ValueError(f"no command {wanted!r} (known: {known})")
    pages = _paginate_help(_render_help_body(specs))
    suffix = "\n(help 2 for more…)" if len(pages) > 1 else ""
    return pages[0] + suffix


async def _prefixrate_handler(ctx: CommandContext, args: List[str]) -> str:
    """Show or set THIS account's send pacing (message-action interval).

    ``prefixrate`` -> the current effective interval; ``prefixrate 2.0``
    -> set it (float seconds; per-account policy override, persisted when the ControlAPI is wired).
    """
    backend = COMMAND_HUB.pacing
    if backend is None:
        raise ValueError("pacing control unavailable on this stack")
    if args:
        try:
            seconds = float(args[0])
        except (TypeError, ValueError):
            raise ValueError(f"prefixrate needs a number of seconds "
                             f"(got {args[0]!r})") from None
        if seconds <= 0:
            raise ValueError("prefixrate must be > 0 seconds")
        await _require_admin(ctx)  # mutation branch: admin-only
        provider = COMMAND_HUB.api_provider
        api = provider() if callable(provider) else provider
        if api is not None:
            # through the ControlAPI so the override is PERSISTED (the
            # engine alone is memory-only) and survives a restart
            previous = backend.get(ctx.account)
            try:
                await api.ratelimit_set(
                    "message", seconds, 0.1, 0.5,
                    level="account", owner=ctx.account)
            except _DOMAIN_ERRORS as exc:
                raise ValueError(str(exc)) from exc
            except ValueError:
                raise
            if previous is None:
                previous = seconds
        else:
            previous = backend.set(ctx.account, seconds)
            if previous is None:
                raise ValueError("the limiter rejected the value")
        note = ""
        if seconds < 2.6:
            note = ("\n⚠ below the measured safe rate (~2.6s) — expect "
                    "429s under sustained sending")
        return (f"prefixrate: {previous:.2f}s → {seconds:.2f}s "
                f"for {ctx.account}{note}")
    current = backend.get(ctx.account)
    if current is None:
        raise ValueError("could not read the current pacing")
    return (f"prefixrate for {ctx.account}: {current:.2f}s between sends "
            f"(prefixrate <seconds> to change)")


async def _require_admin(ctx: CommandContext) -> Any:
    """Admin-only guard for admin* verbs: raises ValueError (renders the
    usage line) when the invoker is not an admin."""
    access = COMMAND_HUB.access
    if access is None:
        raise ValueError("admin commands unavailable on this stack")
    if not await access.is_admin_id(ctx.invoker_id):
        raise ValueError("admins only")
    return access


async def _adminlist_handler(ctx: CommandContext, args: List[str]) -> str:
    access = await _require_admin(ctx)
    admins = await access.admin_ids()
    lines = [f"admins: {', '.join(str(a) for a in admins) or '(none)'}"]
    rows = await access.all_account_rows()
    for name in sorted(rows):
        row = rows[name]
        perms = ", ".join(str(x) for x in row.get("allowed_user_ids", []))
        lines.append(
            f"{name}: {'ADMIN ' if row.get('is_admin') else ''}"
            f"prefix={row.get('prefix_mode', 'mention')}"
            f"{row.get('prefix_symbol', '') and repr(row['prefix_symbol']) or ''} "
            f"perms=[{perms}]")
    return "\n".join(lines)


async def _adminperms_handler(ctx: CommandContext, args: List[str]) -> str:
    access = await _require_admin(ctx)
    if len(args) < 2 or args[1] not in ("add", "remove", "list"):
        raise ValueError("usage: adminperms <account> <add|remove|list> "
                         "[user_id]")
    account, op = args[0], args[1]
    if op == "list":
        ids = await access.allowed_user_ids(account)
        return f"{account} command perms: " + (
            ", ".join(str(x) for x in ids) if ids else "(empty — admins only)")
    if len(args) < 3 or not args[2].isdigit():
        raise ValueError("usage: adminperms <account> <add|remove> <user_id>")
    user_id = int(args[2])
    if op == "add":
        await access.add_allowed(account, user_id)
    else:
        await access.remove_allowed(account, user_id)
    ids = await access.allowed_user_ids(account)
    return f"{account} command perms -> " + (
        ", ".join(str(x) for x in ids) if ids else "(empty — admins only)")


async def _adminprefix_handler(ctx: CommandContext, args: List[str]) -> str:
    access = await _require_admin(ctx)
    if len(args) < 2 or args[1] not in ("mention", "symbol", "both"):
        raise ValueError("usage: adminprefix <account> "
                         "<mention|symbol|both> [symbol]")
    account, mode = args[0], args[1]
    symbol = args[2] if len(args) > 2 else ""
    await access.set_prefix_config(account, mode, symbol)
    return (f"{account} prefix -> {mode}"
            f"{f' {symbol!r}' if symbol else ''} "
            f"(applies on next connect/rebuild)")


# ------------------------------------------- ported remote-control commands
# Framework management verbs, ported 1:1 from the retired
# core.remote_control.RemoteControl allowlist: same grammar, same
# ControlAPI calls, same reply text (one unified command layer — no more
# mention-prefix race when prefix_mode=mention).
@_ported
async def _status_handler(ctx: CommandContext, args: List[str]) -> str:
    """Fleet overview: uptime, task counters, per-account status lines."""
    if args:
        return "'status' takes no arguments"
    api = _framework_api()
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


@_ported
async def _accounts_handler(ctx: CommandContext, args: List[str]) -> str:
    """``accounts [list]|info <n>|start <n>|stop <n>``."""
    api = _framework_api()
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
        await _require_admin(ctx)  # fleet-wide mutation: admin-only
        await api.accounts_start(rest[0])
        return f"started {rest[0]}"
    if sub == "stop" and len(rest) == 1:
        await _require_admin(ctx)  # fleet-wide mutation: admin-only
        await api.accounts_stop(rest[0])
        return f"stopped {rest[0]}"
    return "usage: accounts list|info <n>|start <n>|stop <n>"


@_ported
async def _plugins_handler(ctx: CommandContext, args: List[str]) -> str:
    """``plugins <account> [<plugin> on|off]``."""
    api = _framework_api()
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
    await _require_admin(ctx)  # fleet-wide mutation: admin-only
    await api.plugins_set_enabled(account, plugin, flag == "on")
    return f"{plugin} {'enabled' if flag == 'on' else 'disabled'} on {account}"


@_ported
async def _ratelimit_handler(ctx: CommandContext, args: List[str]) -> str:
    """``ratelimit [show|warnings]`` — rate-limit state."""
    api = _framework_api()
    sub = ctx.parsed.get("view") or (args[0].lower() if args else "show")
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


@_ported
async def _lane_handler(ctx: CommandContext, args: List[str]) -> str:
    """``lane [show]`` — send-lane state (read-only)."""
    api = _framework_api()
    lane = await api.ratelimit_lane()
    if not lane.get("enabled"):
        return "send lane: off (per-account pacing only)"
    state = "PAUSED (all outbound holds)" if lane.get("paused") else "active"
    return (f"send lane: on ({state}) · spacing {lane.get('prefixrate')}s · "
            f"waiting {lane.get('waiting')} · grants {lane.get('grants')}")


@_ported
async def _tasks_handler(ctx: CommandContext, args: List[str]) -> str:
    """``tasks`` — queued/running tasks (first 10)."""
    api = _framework_api()
    rows = await api.tasks_list()
    if not rows:
        return "no queued/running tasks"
    return "\n".join(
        f"· #{t['id']} {t.get('account')}/{t.get('plugin')} {t.get('action')}"
        f" [{t.get('status')}]"
        for t in rows[:10]
    ) + (f"\n(+{len(rows) - 10} more)" if len(rows) > 10 else "")


@_ported
async def _estop_handler(ctx: CommandContext, args: List[str]) -> str:
    """``estop [account]`` — EMERGENCY STOP (always invokable).

    SAFETY: this verb is never gated by visibility or plugin state —
    the executor runs framework commands even on a deactivated account,
    and the (possibly undeliverable) reply survives in the logs.
    """
    api = _framework_api()
    target = ctx.parsed.get("account")
    if target:
        out = await api.accounts_emergency_stop(target, surface="remote")
        ctx.logger.info(
            "account-scoped estop reply may be undeliverable (account "
            "%s is stopped) — reply was: emergency stop %s: %s task(s) "
            "cancelled", target, target, out.get("cancelled_tasks", 0))
        return f"emergency stop {target}: {out.get('cancelled_tasks', 0)} task(s) cancelled"
    out = await api.emergency_stop_all()
    return (
        f"GLOBAL EMERGENCY STOP — {out.get('cancelled_tasks', 0)} task(s) "
        "cancelled, all accounts halted. Restart manually."
    )


@_ported
async def _simulation_handler(ctx: CommandContext, args: List[str]) -> str:
    """``simulation [list]`` — available scenarios (run lives in the
    dashboard)."""
    api = _framework_api()
    scenarios = await api.simulation_list()
    if not scenarios:
        return "no scenarios available"
    return "\n".join(f"· {s['name']}: {s.get('description', '')}" for s in scenarios)


#: framework-declared commands (reserved names; collision policy: a
#: plugin re-declaring one of these is dropped with a warning). The
#: "Framework" group is the retired RemoteControl mention surface.
FRAMEWORK_COMMANDS: Sequence[CommandSpec] = (
    CommandSpec(
        name="status",
        help="Fleet overview and uptime",
        usage="",
        category=FRAMEWORK_CATEGORY,
        handler=_status_handler,
    ),
    CommandSpec(
        name="accounts",
        help="List or start/stop accounts",
        usage="list|info <n>|start <n>|stop <n>",
        category=FRAMEWORK_CATEGORY,
        handler=_accounts_handler,
    ),
    CommandSpec(
        name="plugins",
        help="Plugin states and toggles",
        usage="<account> [<plugin> on|off]",
        category=FRAMEWORK_CATEGORY,
        handler=_plugins_handler,
    ),
    CommandSpec(
        name="ratelimit",
        help="Show policies or warnings",
        usage="show|warnings",
        args=(ArgSpec("view", choices=("show", "warnings"),
                      required=False, default="show"),),
        category=FRAMEWORK_CATEGORY,
        handler=_ratelimit_handler,
    ),
    CommandSpec(
        name="lane",
        help="Send-lane state",
        usage="[show]",
        category=FRAMEWORK_CATEGORY,
        handler=_lane_handler,
    ),
    CommandSpec(
        name="tasks",
        help="Queued and running tasks",
        usage="",
        category=FRAMEWORK_CATEGORY,
        handler=_tasks_handler,
    ),
    CommandSpec(
        name="estop",
        help="EMERGENCY STOP (safety)",
        usage="[account]",
        args=(ArgSpec("account", required=False),),
        category=FRAMEWORK_CATEGORY,
        handler=_estop_handler,
    ),
    CommandSpec(
        name="simulation",
        help="List simulation scenarios",
        usage="list",
        args=(ArgSpec("view", choices=("list",),
                      required=False, default="list"),),
        category=FRAMEWORK_CATEGORY,
        handler=_simulation_handler,
    ),
    CommandSpec(
        name="adminlist",
        help="list admins and per-account access",
        usage="",
        category=SETTINGS_CATEGORY,
        handler=_adminlist_handler,
    ),
    CommandSpec(
        name="adminperms",
        help="manage per-account command perms",
        usage="<account> <add|remove|list> [user_id]",
        category=SETTINGS_CATEGORY,
        handler=_adminperms_handler,
    ),
    CommandSpec(
        name="adminprefix",
        help="set an account's command prefix",
        usage="<account> <mention|symbol|both> [symbol]",
        category=SETTINGS_CATEGORY,
        handler=_adminprefix_handler,
    ),
    CommandSpec(
        name="prefixrate",
        help="show or set send pacing",
        usage="[seconds]",
        category=SETTINGS_CATEGORY,
        handler=_prefixrate_handler,
    ),
    CommandSpec(
        name="pg",
        help="Gateway latency and uptime",
        usage="",
        category=UTILITIES_CATEGORY,
        handler=_pg_handler,
    ),
    CommandSpec(
        name="say",
        help="Repeats the user's message.",
        usage="<text>",
        category=UTILITIES_CATEGORY,
        handler=_say_handler,
    ),
    CommandSpec(
        name="help",
        help="Shows this message",
        usage="[command]",
        category="",  # renders under "No Category" like Mudae
        handler=_help_handler,
    ),
)

_FRAMEWORK_NAMES = frozenset(spec.name for spec in FRAMEWORK_COMMANDS)


def render_help(specs: Sequence[CommandSpec]) -> str:
    """The categorized ``help`` text for one account's spec list.

    Layout (Mudae-style)::

        Roll:
          rollinfo    ...
          rolllist    ...
        Utilities:
          pg          ...
          say         Repeats the user's message.
        No Category:
          help        Shows this message

    Plugin categories first (sorted, title-cased), then Utilities, then
    No Category; inside a category commands are name-sorted and the
    help column is aligned to the longest name. Category groups are
    separated by ONE blank line so :func:`_paginate_help` can split on
    group boundaries — this is the page-1 view of the same rendering
    the ``help`` verb paginates.
    """
    pages = _paginate_help(_render_help_body(specs))
    return pages[0] if len(pages) == 1 else pages[0] + "\n(help 2 for more…)"


#: max description width in the help listing (25 chars + ellipsis)
HELP_DESC_WIDTH = 25
#: soft page budget for the paginated help (Discord limit is 2000)
HELP_PAGE_CHARS = 1750


def _paginate_help(body: str) -> List[str]:
    """Split the rendered help on GROUP boundaries into <=page-budget
    chunks (never mid-group; a single oversized group stays whole)."""
    groups = [g for g in body.split("\n\n") if g.strip()]
    pages: List[str] = []
    current: List[str] = []
    used = 0
    for g in groups:
        if current and used + len(g) + 2 > HELP_PAGE_CHARS:
            pages.append("\n\n".join(current))
            current, used = [], 0
        current.append(g)
        used += len(g) + 2
    if current:
        pages.append("\n\n".join(current))
    return pages or ["no commands registered"]


def _render_help_body(specs: Sequence[CommandSpec]) -> str:
    """Full unpaginated help body (grouped, aligned, truncated).

    Category groups are joined with a BLANK line (``"\\n\\n"``) — the
    boundary :func:`_paginate_help` splits on.
    """
    groups: Dict[str, List[CommandSpec]] = {}
    for spec in specs:
        category = spec.category or NO_CATEGORY
        groups.setdefault(category, []).append(spec)
    plugin_categories = sorted(
        c for c in groups if c not in (UTILITIES_CATEGORY, NO_CATEGORY))
    ordered = plugin_categories + [
        c for c in (UTILITIES_CATEGORY, NO_CATEGORY) if c in groups]
    width = max((len(s.name) for s in specs), default=0) + 2
    width = max(width, 10)
    blocks: List[str] = []
    for category in ordered:
        header = category if category == NO_CATEGORY else category.upper()
        lines: List[str] = [header]
        for spec in sorted(groups[category], key=lambda s: s.name):
            desc = spec.help
            if len(desc) > HELP_DESC_WIDTH:
                desc = desc[: HELP_DESC_WIDTH - 1].rstrip() + "…"
            lines.append(f"    {spec.name.ljust(width)}{desc}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "no commands registered"


# --------------------------------------------------------------------- hub
@dataclass(frozen=True)
class RegisteredCommand:
    """One registry entry: the (bound) spec + the plugin it came from."""

    spec: CommandSpec
    #: plugin name (e.g. "probe") or FRAMEWORK_PLUGIN for pg/say/help
    plugin: str


class SelfCommandHub:
    """Process-wide account → command-surface registry.

    ``pacing`` is the optional PacingBackend registered by the bootstrap
    (prefixrate command); None until the limiter is wired.

    The plugin manager publishes; each account's live client subscribes;
    invocations route back to the manager. All state is guarded for the
    reconnect cycle (a new client replaces the old subscriber) and for
    test isolation (a new manager rebinds; stale subscribers raising are
    contained).
    """

    def __init__(self) -> None:
        self._manager: Any = None
        #: account -> registry entries (spec order = registration order)
        self._entries: Dict[str, List[RegisteredCommand]] = {}
        #: account -> the live client's rebuild callback (latest wins)
        self._subscribers: Dict[str, Callable[[List[CommandSpec]], None]] = {}
        #: account -> zero-arg stats provider (latency/uptime, set by the
        #: client when it subscribes)
        self._stats: Dict[str, Callable[[], Dict[str, Any]]] = {}
        #: optional PacingBackend (bootstrap registers; prefixrate uses)
        self.pacing: Any = None
        #: optional AccessManager (bootstrap registers; admin* verbs use)
        self.access: Any = None
        #: zero-arg ControlAPI provider (bootstrap registers right after
        #: the API is built; the ported Framework verbs use it — the same
        #: late-bound seam as pacing/access)
        self.api_provider: Any = None

    # ---------------------------------------------------------- bindings
    def bind(self, manager: Any) -> None:
        """Bind the process's plugin manager (last-wins; tests rebind)."""
        self._manager = manager

    def unbind(self, manager: Any) -> None:
        """Drop the binding if THIS manager still owns it (shutdown)."""
        if self._manager is manager:
            self._manager = None

    @property
    def bound_manager(self) -> Any:
        """The currently bound plugin manager (None when unbound).

        Read-only view for callers that must save/restore the process
        binding around a temporary manager (the simulation runner)."""
        return self._manager

    # ----------------------------------------------------------- publish
    def refresh(self, account: str) -> None:
        """Recompute one account's registry and notify its subscriber.

        Called by the plugin manager from the SAME paths that re-attach
        plugins (assignment mutations, enable toggles) — so the client's
        Bot registrations always track the assigned ∧ enabled set.
        """
        entries = self._collect(account)
        self._entries[account] = entries
        subscriber = self._subscribers.get(account)
        if subscriber is None:
            return
        try:
            subscriber([entry.spec for entry in entries])
        except Exception:
            # a replaced/reconnecting client: contained, never fatal —
            # the NEXT subscriber (fresh connect) pulls fresh state.
            LOG.debug("self-command subscriber failed for %r", account,
                      exc_info=True)

    def forget(self, account: str) -> None:
        """Drop every trace of an account (detached/removed cascade)."""
        self._entries.pop(account, None)
        self._subscribers.pop(account, None)
        self._stats.pop(account, None)

    def _collect(self, account: str) -> List[RegisteredCommand]:
        """Framework commands first (reserved), then the manager's
        plugin entries — first registration of a name wins."""
        entries: List[RegisteredCommand] = []
        for spec in FRAMEWORK_COMMANDS:
            entries.append(RegisteredCommand(spec=spec, plugin=FRAMEWORK_PLUGIN))
        manager = self._manager
        if manager is None:
            return entries
        collector = getattr(manager, "command_entries", None)
        if collector is None:
            return entries
        try:
            plugin_entries = collector(account)
        except Exception:
            LOG.exception("self-command collection failed for %r", account)
            return entries
        for spec, plugin in plugin_entries:
            if spec.name in _FRAMEWORK_NAMES:
                LOG.warning(
                    "self-command %r declared by plugin %r collides with a "
                    "reserved framework name — the plugin's is dropped",
                    spec.name, plugin)
                continue
            entries.append(RegisteredCommand(spec=spec, plugin=plugin))
        return entries

    # --------------------------------------------------------- subscribe
    def subscribe(
        self,
        account: str,
        rebuild: Callable[[List[CommandSpec]], None],
        stats_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        """A live client subscribes: register the rebuild callback, then
        REFRESH from the bound manager and deliver the result — a fresh
        connect never serves entries a previous manager computed (test
        boots reuse account names; production restarts are new processes)."""
        self._subscribers[account] = rebuild
        if stats_provider is not None:
            self._stats[account] = stats_provider
        self.refresh(account)

    def unsubscribe(
        self,
        account: str,
        rebuild: Optional[Callable[[List[CommandSpec]], None]] = None,
    ) -> None:
        """The client disconnected/stopped: drop its callback and stats
        provider (a reconnect re-subscribes both).

        ``rebuild`` makes the drop OWNERSHIP-aware: a stale client's late
        disconnect must not remove the subscription a newer client
        already installed under the same account name."""
        if rebuild is not None and self._subscribers.get(account) != rebuild:
            return
        self._subscribers.pop(account, None)
        self._stats.pop(account, None)

    # ------------------------------------------------------------- reads
    def entries(self, account: str) -> List[RegisteredCommand]:
        """Current registry entries (fresh recompute — never stale)."""
        if account not in self._entries:
            self.refresh(account)
        return list(self._entries.get(account, ()))

    def specs(self, account: str) -> List[CommandSpec]:
        """Just the specs (help rendering / registration)."""
        return [entry.spec for entry in self.entries(account)]

    def runtime_stats(self, account: str) -> Dict[str, Any]:
        """Latest latency/uptime from the account's client ({} = none)."""
        provider = self._stats.get(account)
        if provider is None:
            return {}
        try:
            stats = provider() or {}
            return stats if isinstance(stats, dict) else {}
        except Exception:
            return {}

    # ---------------------------------------------------------- invoke
    async def invoke(
        self,
        account: str,
        name: str,
        args: List[str],
        channel_id: int,
        *,
        guild_id: Optional[int] = None,
        invoker_id: Optional[int] = None,
        raw: str = "",
    ) -> None:
        """Route one invocation to the bound manager (fully contained).

        The manager's executor builds the CommandContext, runs the
        handler with plugin-grade isolation and sends the reply through
        the paced pipeline. Unknown commands and an unbound manager are
        dropped quietly (the Bot layer never dispatches unknown names
        anyway — CommandNotFound).
        """
        manager = self._manager
        if manager is None:
            LOG.debug("self-command %r dropped: no manager bound", name)
            return
        execute = getattr(manager, "execute_self_command", None)
        if execute is None:
            return
        try:
            await execute(
                account, name, args, channel_id,
                guild_id=guild_id, invoker_id=invoker_id, raw=raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            # the executor is responsible for its own containment; this
            # guard exists so a BROKEN executor can never kill the Bot's
            # command dispatch task
            LOG.exception("self-command execution failed: %s!%s", account, name)


__all__ = [
    "SelfCommandHub", "RegisteredCommand", "FRAMEWORK_COMMANDS",
    "FRAMEWORK_PLUGIN", "UTILITIES_CATEGORY", "SETTINGS_CATEGORY",
    "FRAMEWORK_CATEGORY", "NO_CATEGORY",
    "MAX_REPLY_CHARS", "render_help", "COMMAND_HUB",
]


#: the process-wide hub (one framework per process; managers bind to it)
COMMAND_HUB = SelfCommandHub()
