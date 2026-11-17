"""PluginCogHost — the framework-owned cog host (C1 of the command
convergence; owner-locked 2026-11-14).

THE model: plugins ARE cogs for their command face. The host builds one
cog instance per (account, plugin) with the services facade injected,
and assignment ∧ enabled gating becomes add_cog/remove_cog — the
library's own hot-reload IS the rebuild mechanism.

What the host provides a plugin cog (all injected — plugins never
import adapters):
  - `ctx.services`     — the paced PluginServices facade (loops ride
    ctx.services.send_message; ctx.send stays library-native and is
    for COMMAND RESPONSES ONLY — the documented discipline)
  - `self.to_rich(message)` — the DTO/audit feature module: the same
    frozen DiscordMessage the framework builds (embeds, components,
    buttons parsed once), plus optional inbound audit firing —
    first-hand event handling without per-plugin parsing walkers
  - `self.audit_rich(message)` — fire the inbound audit row for a
    message handled first-hand

What the host does NOT do: wire `@Cog.listener()` behind any framework
gate — plugin listeners are first-class and run alongside the
framework intake cog, not behind it (the intake cog handles the
framework's own audit + bus publication).

The host is deliberately TRANSPARENT: a plugin cog written against the
library works unmodified; the host only injects the framework toolbox
and manages the (account, plugin) lifecycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.plugin_base import PluginServices

LOG = logging.getLogger("libzent.cogs")

__all__ = ["CogServices", "PluginCogHost"]


class RichConverter(Protocol):
    """The to_rich seam: the adapter's convert_message, injected."""

    def __call__(self, message: Any) -> Any: ...


class AuditPublisher(Protocol):
    """The inbound-audit seam: publishing the typed MessageReceived
    event on the bus is what the framework intake does — the toolbox
    hands first-hand handlers the same effect."""

    def __call__(self, event: Any) -> None: ...


class CogServices:
    """The toolbox injected into every plugin cog instance.

    Carries the event-side utilities AND the paced facade: the host
    binds the per-(account, plugin) PluginServices at attach time and
    the client's before_invoke lifts it onto ``ctx.services`` — the
        documented command seam (roadmap #4)."""

    def __init__(
        self,
        *,
        account: str,
        plugin: str,
        to_rich: RichConverter | None = None,
        audit_publisher: AuditPublisher | None = None,
        inbound_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._account = account
        self._plugin = plugin
        self._to_rich = to_rich
        self._audit = audit_publisher
        self._inbound_factory = inbound_factory
        self._paced: PluginServices | None = None

    @property
    def account(self) -> str:
        return self._account

    @property
    def plugin(self) -> str:
        return self._plugin

    def to_rich(self, message: Any) -> Any:
        """The frozen DiscordMessage the framework itself builds —
        embeds, components, buttons parsed ONCE. Raises RuntimeError
        when no converter is wired (unwired/test stacks): a listener
        that needs it should degrade explicitly, not parse by hand."""
        if self._to_rich is None:
            raise RuntimeError(
                f"to_rich is not wired on this stack (plugin {self._plugin!r})"
                " — the cog host needs the adapter's converter"
            )
        return self._to_rich(message)

    def audit_rich(self, message: Any) -> None:
        """Fire the inbound audit for a first-hand-handled message —
        the same row the framework intake would record."""
        if self._audit is None or self._inbound_factory is None:
            return  # unwired stack: the audit simply doesn't fire
        dto = self.to_rich(message)
        self._audit(self._inbound_factory(account=self._account, message=dto))

    @property
    def paced(self) -> PluginServices | None:
        """The per-(account, plugin) paced facade, bound at attach
        time by the host. None until the account manager hands the
        facade over — consumers treat None as unwired (fail-closed),
        never as a fallback to some other account's pipeline."""
        return self._paced

    def bind_paced(self, paced: PluginServices | None) -> None:
        """Bind (or rebind) the paced facade for this toolbox. Called
        by the host on attach; detach pops the whole toolbox, so the
        binding never outlives the (account, plugin) cog it belongs
        to."""
        self._paced = paced


class PluginCogHost:
    """Builds and manages plugin cog instances per (account, plugin).

    The host is the C1 seam between the plugin manager's attach/
    detach lifecycle and the library's add_cog/remove_cog: the manager
    calls :meth:`attach`/:meth:`detach` and the Bot's command surface
    follows the assignment ∧ enabled state by construction."""

    def __init__(
        self,
        *,
        to_rich: RichConverter | None = None,
        audit_publisher: AuditPublisher | None = None,
        inbound_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._to_rich = to_rich
        self._audit = audit_publisher
        self._inbound_factory = inbound_factory
        #: (account, plugin) -> the injected CogServices
        self._services: dict[tuple[str, str], CogServices] = {}
        #: (account, plugin) -> the attached cog INSTANCE (detach needs
        #: its registered name; the library keys cogs by class name)
        self._cogs: dict[tuple[str, str], Any] = {}
        #: bots we have attached cogs to (account -> bot); add_cog is
        #: per-Bot and each account owns exactly one Bot
        self._bots: dict[str, Any] = {}

    def services_for(self, account: str, plugin: str) -> CogServices:
        """The toolbox for one (account, plugin) cog — created lazily
        so cog classes can grab it in __init__ via the host reference."""
        key = (account, plugin)
        if key not in self._services:
            self._services[key] = CogServices(
                account=account,
                plugin=plugin,
                to_rich=self._to_rich,
                audit_publisher=self._audit,
                inbound_factory=self._inbound_factory,
            )
        return self._services[key]

    def rebind_paced(self, account: str, plugin: str, paced: PluginServices) -> None:
        """Rebind the paced facade on an EXISTING toolbox — a no-op
        when the (account, plugin) cog was never hosted. attach()
        creates; rebind refreshes: the account manager calls this after
        _attach_plugins rebuilt the runtimes and dropped the services
        cache, so a hosted cog's command path keeps sharing the live
        settings dict with the event handlers (CodeRabbit pre-review
        2026-11-17: binding only at connect left the FIRST start's
        cogs on a pre-attach facade that set_setting stopped updating)."""
        toolbox = self._services.get((account, plugin))
        if toolbox is not None:
            toolbox.bind_paced(paced)

    def hosted_plugins(self, account: str) -> set[str]:
        """The plugins whose cogs are currently hosted on this account
        — the reconciliation input: the attach flow diffs this against
        the current runtime set to decide what to host, unhost, or
        rebind."""
        return {plugin for (acct, plugin) in self._cogs if acct == account}

    async def attach(
        self,
        bot: Any,
        account: str,
        plugin: str,
        cog_class: Any,
        paced: PluginServices | None = None,
    ) -> Any:
        """Build the cog instance with its toolbox injected and add it
        to the account's Bot (the fork's add_cog is a coroutine). The
        cog class receives ``services`` (the CogServices toolbox) as
        its sole keyword argument; ``paced`` is the per-(account,
        plugin) facade, bound to the toolbox so the client hook can
        lift it onto ``ctx.services`` (roadmap #4)."""
        services = self.services_for(account, plugin)
        services.bind_paced(paced)
        cog = cog_class(services=services)
        await bot.add_cog(cog)
        self._cogs[(account, plugin)] = cog
        self._bots[account] = bot
        LOG.debug("cog attached: %s/%s", account, plugin)
        return cog

    async def detach(self, account: str, plugin: str) -> bool:
        """Remove the plugin's cog from the account's Bot (remove_cog
        is a coroutine in the fork). The library names a cog by its
        class name; detach resolves it from the tracked instance."""
        bot = self._bots.get(account)
        cog = self._cogs.pop((account, plugin), None)
        self._services.pop((account, plugin), None)
        if bot is None or cog is None:
            return False
        name = getattr(cog, "__cog_name__", None) or type(cog).__name__
        await bot.remove_cog(name)
        LOG.debug("cog detached: %s/%s", account, plugin)
        return True
