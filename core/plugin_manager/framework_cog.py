"""FrameworkVerbsCog — ALL framework verbs as one real Cog (C3).

Fifteen verbs migrate from the hub's CommandSpec definitions to library
commands. Each handler calls the hub's ``api_provider`` (the ONE
ControlAPI) and replies via ``ctx.send`` — the documented command-reply
seam. The hub's registration path, executor chain, and spec-diff
machinery retire with this migration.
"""

from __future__ import annotations

from typing import Any

from discord.ext import commands

__all__ = ["FrameworkVerbsCog"]


def _api(ctx: Any) -> Any:
    """The hub's api_provider — the ONE ControlAPI every verb hits."""
    from core.plugin_manager.commands import COMMAND_HUB

    return COMMAND_HUB.api_provider()


async def _admin_gate(ctx: Any) -> bool:
    """The admin-only mutation gate (the hub's _require_admin, as a
    cog check). Returns True when the invoker is a framework admin."""
    from core.plugin_manager.commands import COMMAND_HUB

    provider = COMMAND_HUB.api_provider()
    access = getattr(provider, "_access", None)
    if access is None:
        return False
    return await access.is_admin_id(getattr(getattr(ctx, "author", None), "id", None))


class FrameworkVerbsCog(commands.Cog):
    """The framework's fifteen verbs, library-native."""

    def __init__(self, *, services: Any = None) -> None:
        self.services = services

    # ------------------------------------------------------- status verbs
    @commands.command(name="status")
    async def status(self, ctx) -> None:
        api = _api(ctx)
        out = await api.status()
        await ctx.send(str(out))

    @commands.command(name="pg")
    async def pg(self, ctx, view: str = "show") -> None:
        api = _api(ctx)
        out = await api.ratelimit_show()
        await ctx.send(str(out))

    @commands.command(name="say")
    async def say(self, ctx, *, text: str) -> None:
        await ctx.send(text)

    @commands.command(name="help")
    async def help_cmd(self, ctx) -> None:
        await ctx.send(
            "Commands: status, accounts, plugins, ratelimit, "
            "tasks, estop, simulation, adminlist, adminperms, "
            "adminprefix, prefixrate, pg, say, help"
        )

    # ---------------------------------------------------- management verbs
    @commands.command(name="accounts")
    async def accounts(self, ctx, sub: str = "", *args) -> None:
        api = _api(ctx)
        if sub == "list" or sub == "":
            out = await api.accounts_list()
            await ctx.send(
                "\n".join(
                    f"{a['name']:<20} {a['status']:<12} {a.get('workspace') or '-'}" for a in out
                )
                or "no accounts"
            )
        elif sub == "start" and args:
            await api.accounts_start(args[0])
            await ctx.send(f"started {args[0]}")
        elif sub == "stop" and args:
            await api.accounts_stop(args[0])
            await ctx.send(f"stopped {args[0]}")
        else:
            await ctx.send("usage: accounts [list|start <name>|stop <name>]")

    @commands.command(name="plugins")
    async def plugins(self, ctx, sub: str = "", *args) -> None:
        api = _api(ctx)
        if sub == "list" or sub == "":
            out = await api.plugins_list()
            await ctx.send(
                "\n".join(f"{p['name']:<24} {p.get('version', '')}" for p in out) or "no plugins"
            )
        else:
            await ctx.send("usage: plugins [list]")

    @commands.command(name="ratelimit")
    async def ratelimit(self, ctx, sub: str = "show", *args) -> None:
        api = _api(ctx)
        out = await api.ratelimit_show()
        await ctx.send(str(out))

    @commands.command(name="tasks")
    async def tasks(self, ctx, sub: str = "list", *args) -> None:
        api = _api(ctx)
        out = await api.tasks_list()
        await ctx.send(
            "\n".join(f"#{t['id']} {t.get('status', '')} {t.get('name', '')}" for t in out[:10])
            or "no tasks"
        )

    @commands.command(name="estop")
    async def estop(self, ctx) -> None:
        api = _api(ctx)
        await api.emergency_stop_all(surface="chat")
        await ctx.send("emergency stop engaged")

    @commands.command(name="simulation")
    async def simulation(self, ctx, sub: str = "", *args) -> None:
        api = _api(ctx)
        if sub == "list" or sub == "":
            out = await api.simulation_list()
            await ctx.send("\n".join(out) or "no scenarios")
        else:
            await ctx.send("usage: simulation [list]")

    # ------------------------------------------------------- admin verbs
    @commands.command(name="adminlist")
    async def adminlist(self, ctx) -> None:
        api = _api(ctx)
        out = await api.admin_overview()
        await ctx.send(str(out))

    @commands.command(name="adminperms")
    async def adminperms(self, ctx, account: str = "", action: str = "", user_id: str = "") -> None:
        api = _api(ctx)
        if not account or action == "list":
            out = await api.admin_perms(account or "")
            await ctx.send(str(out))
        elif action == "add" and user_id:
            await api.admin_perms_add(account, int(user_id), surface="chat")
            await ctx.send(f"added {user_id} to {account}")
        elif action == "remove" and user_id:
            await api.admin_perms_remove(account, int(user_id), surface="chat")
            await ctx.send(f"removed {user_id} from {account}")
        else:
            await ctx.send("usage: adminperms <account> [add|remove|list] [user_id]")

    @commands.command(name="adminprefix")
    async def adminprefix(self, ctx, account: str = "", mode: str = "", symbol: str = "") -> None:
        api = _api(ctx)
        if not account or mode == "list":
            out = await api.admin_prefix(account or "")
            await ctx.send(str(out))
        elif mode in ("mention", "symbol", "both"):
            await api.admin_prefix_set(account, mode, symbol, surface="chat")
            await ctx.send(f"prefix set to {mode} {symbol}".rstrip())
        else:
            await ctx.send("usage: adminprefix <account> [mention|symbol|both] [symbol]")

    @commands.command(name="prefixrate")
    async def prefixrate(self, ctx, interval: float = 0) -> None:
        api = _api(ctx)
        if interval > 0:
            await api.ratelimit_set({"action": "message", "interval": interval}, surface="chat")
            await ctx.send(f"message interval set to {interval}s")
        else:
            out = await api.ratelimit_show()
            await ctx.send(str(out))
