"""Account manager (A6): Account, AccountRuntime, AccountManager, DiscordCache wiring.

════════════════════════════════════════════════════════════════════════
API BOUNDARY CONTRACT (architecture spec) — audited & enforced
════════════════════════════════════════════════════════════════════════
This module is the framework: event routing, account control, task
orchestration, lifecycle. It NEVER initiates a Discord API call for its own
purposes. The COMPLETE inventory of client touches (each demand-driven):

  1. ``connect()/disconnect()``          — gateway lifecycle (account control)
  2. ``client.cached_snapshot()``        — memory-only read of what the client
                                           already holds; ZERO API calls
  3. ``send_hook`` → ``client.send_message`` — EXECUTES a task a PLUGIN
                                           submitted (orchestration; paced by
                                           the RateLimiter before it runs)
  4. ``resolve_dm_channel`` → ``client.open_dm`` — ONLY on a DM-send cache
                                           miss, for a plugin-demanded send
  5. ``services.fetch_user`` → ``client.fetch_user`` — PLUGIN-facing,
                                           on-demand, DB-cached per account
  6. ``_maybe_typing`` → ``client.send_typing`` — the ONE deliberate,
                                           opt-in exception: a send-decoration
                                           policy (typing_probability, default
                                           0.0 = never) — no plugin demands it,
                                           so it ships disabled

There is NO prefetching, NO "just in case" fetching, and NO API-derived
state beyond what a live send/flow actually consumed. ``details()`` reads
exclusively from the ingested READY snapshot + repos. Rate-scope metadata
(``_sync_group_apply`` / ``_sync_group_scope``) is pure plumbing — no API
involved.
════════════════════════════════════════════════════════════════════════

One :class:`Account` per Discord user account, split into PERSISTENT state
(``record`` — database/models.AccountRecord) and RUNTIME state
(``runtime`` — client, status, cache, supervisor; never persisted).

Lifecycle (ported from ``git show HEAD:core/container.py`` + ``core/account.py``,
audited through 3 review rounds):

  * ``start`` — resolve the token (.env by convention key), build a client via
    the factory, wire callbacks BEFORE connect, connect, ingest the client's
    cached snapshot into the per-account DiscordCache, attach+activate the
    account's plugins, publish ``AccountConnected``, spawn the reconnect
    supervisor (when enabled by the account's framework settings).
  * supervisor — watches a per-account ``lost`` asyncio.Event; on an
    UNEXPECTED drop (never on deliberate stops) reconnects with exponential
    backoff, a FRESH client per attempt, cache re-ingest and another
    ``AccountConnected``. KeyboardInterrupt/SystemExit never kill the process.
  * ``stop`` — idempotent: mark stop_requested, deactivate plugins FIRST
    (hooks still see the cache), disconnect the client (None-safe), reap the
    supervisor with a bounded 1s wait then cancel, status stopped, publish
    ``AccountDisconnected(reason="stopped")``.

Duck typing: ``plugin_manager`` / ``scheduler`` / ``limiter`` are parallel
deliverables (A3/A5/A4) — they are called ONLY via the surfaces pinned in
INTEGRATION.md, with tolerant fallbacks where a collaborator may legitimately
lack a feature. Nothing imports those packages at runtime.

Service wiring note for the lead: ``plugin_manager.activate_account`` is
called with a SERVICES FACTORY (``factory(plugin_name) -> AccountPluginServices``),
not a bare services object — per-plugin task submission needs the plugin
name. This matches the A6 hand-off contract and should be reconciled with A3.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..errors import AccountError, CredentialsError, SchedulerError
from ..event_bus import (
    AccountConnected,
    AccountDisconnected,
    AccountFailed,
    EventBus,
    MessageReceived,
)

#: account names share the profile namespace style (may start with a digit)
from ..names import ACCOUNT_NAME_RE as NAME_RE
from .cache import DiscordCache, invalidate_dm_channel, resolve_dm_channel
from .services import AccountPluginServices, MessageWaiterRegistry, TaskAckRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    from adapters.base import DiscordMessage
    from database.models import AccountRecord

    from ..credentials import Credentials
    from ..settings_manager import SettingsManager


def _forget_account_loggers(name: str) -> None:
    """FINAL-PHASE (G3/R4): prune a removed account's loggerDict subtree.

    Per-account logger NAMES were kept deliberately (attribution is
    name-derived end-to-end: bridge name→source, dashboard display, CLI,
    DB rows — migrating to structured context would orphan it), but
    stdlib never frees them, so distinct-name churn grew loggerDict
    forever (+~3 entries per account ever seen). Pruning OUR OWN
    ``libzent.<name>*`` subtree in the removal cascade bounds
    retention to LIVE accounts — the same forget-idiom as every other
    mirror (K14/K15/F1).

    Safety (constraint: never blind-manipulate loggerDict): we delete
    only names we created, under our own namespace, with the exact
    shapes the two creation sites emit; ``_clear_cache()`` (3.11+) then
    invalidates stdlib's cached placeHolder/parent lookups. A
    still-referenced Logger object keeps working (propagation follows
    the OBJECT graph); only the dict entry is dropped, so a later
    getLogger of the same name re-creates it cleanly.
    """
    try:
        manager = logging.Logger.manager
        prefix = f"libzent.{name}"
        doomed = [
            key
            for key in manager.loggerDict
            if key == prefix or key.startswith(prefix + ".") or key == f"libzent.adapter.{name}"
        ]
        for key in doomed:
            manager.loggerDict.pop(key, None)
        if doomed:
            clear = getattr(manager, "_clear_cache", None)
            if callable(clear):
                clear()
    except Exception:  # logging internals drift: never break removal
        pass


#: fallback reconnect policy when the settings manager is unavailable/blank
_DEFAULT_RECONNECT = {"initial": 1.0, "max": 60.0, "multiplier": 2.0}

#: FINAL-PHASE (F4): start_all concurrency width. The IdentifyGate is the
#: true serializer (1 IDENTIFY / interval regardless of width), so this
#: only bounds how many clients are built-and-parked concurrently: 4
#: lanes + ~5 storm-reconnect waiters stays under the gate's max_wait.
_START_ALL_WIDTH = 4

#: exponent cap for the reconnect backoff (``initial * multiplier**attempt``).
#: float ** int raises OverflowError once the result leaves the float range
#: (2.0 ** 1025, ~21h into an outage at the default policy) — clamping the
#: exponent keeps the product astronomically above any sane ``max`` while
#: making the default-config long-outage case unrepresentable. A hostile
#: huge multiplier is still caught at the computation site (see
#: ``_reconnect_loop``).
_BACKOFF_EXPONENT_CAP = 32

#: login-failure text markers used to classify INVALID_CREDENTIAL (the live
#: client surfaces library LoginFailures as plain AccountError strings, so
#: message text is the portable detection seam — documented heuristic)
_AUTH_ERROR_MARKERS = ("bad token", "improper token")

#: a bare HTTP 401 as its own token — NOT a substring of an account name
#: or id ("alt401", "1401234") that merely rides in an error message
_AUTH_401_RE = re.compile(r"(?<![\w-])401(?![\w-])")


def _looks_like_auth_error(exc: BaseException) -> bool:
    """True when an exception's text looks like a credential rejection.

    Matches 'bad token', '401' or 'improper token' (case-insensitive) in
    the exception message or any chained cause — the strings discord
    client libraries emit for rejected tokens.
    """
    seen = 0
    walker: BaseException | None = exc
    while walker is not None and seen < 5:  # bounded cause chain walk
        text = str(walker).lower()
        if any(marker in text for marker in _AUTH_ERROR_MARKERS) or _AUTH_401_RE.search(text):
            return True
        walker = walker.__cause__ or walker.__context__
        seen += 1
    return False


class AccountStatus(str, Enum):
    """Lifecycle states of one account's connection."""

    CREATED = "created"
    CONNECTING = "connecting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"
    FAILED = "failed"
    #: no credential resolvable for the account (DB absent AND legacy .env
    #: key absent) — fix by storing a credential, not by restarting
    MISSING_CREDENTIAL = "missing_credential"
    #: the credential was REJECTED by Discord at login/startup (distinct
    #: from a network failure: the token itself must be replaced)
    INVALID_CREDENTIAL = "invalid_credential"


class _StaleGeneration(Exception):
    """PHASE-4: a lifecycle operation resumed after ownership changed.

    Raised by ``_connect_client`` (and honored by start/reconnect callers)
    when ``rt.generation`` moved on while the operation was suspended. The
    message deliberately avoids auth-shaped words so ``_looks_like_auth_error``
    text matching can never classify it as a credential failure.
    """

    def __init__(self, name: str, generation: int) -> None:
        super().__init__(
            f"lifecycle generation changed for account {name!r} (captured {generation})"
        )
        self.account = name
        self.generation = generation


@dataclass
class AccountRuntime:
    """RUNTIME state of one account (never persisted).

    ``lost`` extends the pinned field set: an asyncio.Event set by the
    disconnect callback on UNEXPECTED drops; the supervisor waits on it.
    ``started_at`` uses ``time.monotonic()`` (uptime is wall-clock-jump safe).
    """

    status: AccountStatus = AccountStatus.CREATED
    client: Any = None
    cache: DiscordCache | None = None
    started_at: float | None = None
    last_error: str | None = None
    user_id: int | None = None
    supervisor: asyncio.Task[None] | None = None
    stop_requested: bool = False
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # M2 (flap breaker): monotonic timestamp of the last successful READY
    # and the recent SHORT-LIVED sessions (< 90s). When Discord culls an
    # account's sessions (young-account anti-abuse treatment), each
    # reconnect SUCCEEDS — so the attempt counter resets to 1 every cycle
    # and the loop would hammer IDENTIFYs forever at minimum backoff.
    session_started: float = 0.0
    flaps: list[float] = field(default_factory=list)
    # PHASE-4 (S1/S2 hardening): monotonic lifecycle generation. Bumped at
    # every ownership change (start entry, stop inside the lock, remove,
    # estop). Every async lifecycle path captures it BEFORE an await and
    # validates AFTER: a stale operation aborts and cleans up what it
    # created instead of becoming authoritative over a newer generation.
    # This expresses what stop_requested cannot: an old start must never
    # resurrect a stopped account, an old stop must never clobber a newer
    # start's terminal state, and a stale supervisor must never reconnect
    # a newer generation's client.
    generation: int = 0

    def require_cache(self) -> DiscordCache:
        """The per-account cache. ``Account.__post_init__`` guarantees it
        exists for every constructed account; a None here means a
        hand-built runtime bypassed Account — a programming error, not
        a state to tolerate."""
        cache = self.cache
        if cache is None:
            raise RuntimeError("AccountRuntime.cache is missing — runtime built outside Account")
        return cache


@dataclass
class Account:
    """First-class account object: persistent record + runtime + plugin view.

    ``plugins`` caches the last known per-plugin runtime info
    (``{name: {"enabled","last_error","error_count"}}``); it is refreshed from
    ``plugin_manager.states()`` whenever a status snapshot is built and the
    plugin manager has the account, and keeps the last known values otherwise.
    ``credential_source`` mirrors ``CredentialService.status(name)`` ("db" /
    "env", None when no credential service is wired) for display snapshots.
    ``assigned_plugins`` is the in-memory mirror of the ``account_plugins``
    table rows (the CAPABILITY source of truth): loaded in bulk at boot,
    rewritten at every assignment mutation, read by the attach path — never
    a per-attach repository roundtrip. ``plugin_profiles`` mirrors the
    same rows' ``profile`` column (the bound settings profile per plugin,
    None = defaults) for the status poll and profile-binding reads.
    """

    record: AccountRecord
    runtime: AccountRuntime = field(default_factory=AccountRuntime)
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)
    credential_source: str | None = None
    assigned_plugins: list[str] = field(default_factory=list)
    #: in-memory mirror of the ``account_plugins.profile`` column:
    #: plugin -> bound profile name (None = plugin defaults). Loaded in
    #: bulk at boot, rewritten at every assignment/binding mutation.
    plugin_profiles: dict[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Every account owns exactly one DiscordCache for its whole life."""
        if self.runtime.cache is None:
            self.runtime.cache = DiscordCache(self.record.name)

    # -------------------------------------------------------------- status
    def status_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot (accounts list; see details() for the full page)."""
        rt = self.runtime
        client = rt.client
        latency = getattr(client, "latency_ms", None) if client is not None else None
        uptime = None
        if rt.started_at is not None and rt.status == AccountStatus.RUNNING:
            uptime = round(max(0.0, time.monotonic() - rt.started_at), 1)
        return {
            "id": self.record.id,
            "name": self.record.name,
            "status": rt.status.value,
            "user_id": rt.user_id,
            "enabled": self.record.enabled,
            "sync_group": self.record.sync_group,
            "workspace": self.record.workspace,
            "assigned_plugins": list(self.assigned_plugins),
            "plugin_profiles": dict(self.plugin_profiles),
            "latency_ms": latency,
            "uptime_s": uptime,
            "last_error": rt.last_error,
            "credential": self.credential_source,
            "plugins": {name: dict(info) for name, info in sorted(self.plugins.items())},
        }

    def channels_snapshot(self) -> list[dict[str, Any]]:
        """Text-channel snapshot from the LIVE client ([] when offline).

        Read-only over what the client already holds in memory (the
        READY cache) — zero API calls. Never raises: an unstarted
        account, a client without the method (duck-typed doubles) or a
        snapshot failure all read as [] (the caller offers manual
        channel-ID entry instead — picker UX, Phase 2).
        """
        client = self.runtime.client
        if client is None:
            return []
        getter = getattr(client, "channels_snapshot", None)
        if not callable(getter):
            return []
        try:
            return list(getter() or [])
        except Exception:  # a broken client view must never take a read down
            return []


class AccountManager:
    """Owns every account: registry, lifecycle, send hook, details page."""

    def __init__(
        self,
        bus: EventBus,
        logger: Any,
        accounts_repo: Any,
        plugin_manager: Any,
        scheduler: Any,
        settings: SettingsManager,
        audit: Any,
        client_factory: Callable[[str, str], Any],
        credentials: Credentials,
        *,
        limiter: Any = None,
        supervisor_tick: float = 0.2,
        api_cache_repo: Any = None,
        credential_service: Any = None,
        cooldowns: Any = None,
        plugin_state: Any = None,
        plugin_data: Any = None,
        lifecycle: Any = None,
        sync_groups: Any = None,
        account_plugins: Any = None,
    ) -> None:
        """``limiter`` (optional) enriches details() with effective rate data;
        ``supervisor_tick`` is the supervisor's poll granularity in seconds;
        ``api_cache_repo`` (optional) persists resolved Discord entities
        (DM channels, users) per account so restarts never re-fetch them;
        ``credential_service`` (optional CredentialService) resolves tokens
        DB-first with the legacy .env as fallback and reports the
        db/env/missing source for status pages; ``cooldowns`` (optional
        CooldownService) and ``plugin_state`` (optional PluginStateService)
        are handed to the per-plugin services facades (plugin API);
        ``lifecycle`` (optional LifecycleGovernor, PHASE-4) rate-limits
        start/stop thrashing per account and fleet-wide — None disables
        limiting (tests/simulators construct managers without it);
        ``sync_groups`` (optional SyncGroupManager) owns group membership:
        when wired, the group-scope plumbing of add/remove/restore is
        delegated to it (AccountManager -> SyncGroupManager ->
        RateLimitManager); unwired stacks keep the legacy direct plumbing;
        ``account_plugins`` (optional AccountPluginsRepository,
        ``db.account_plugins``) is the CAPABILITY source of truth: the
        account_plugins table rows decide which plugins an account runs
        (ProfileManager no longer does). Unwired stacks degrade to ZERO
        attached plugins (fail-closed), never to "all plugins".

        The constructor does NO repository I/O — persisted accounts are
        restored by awaiting :meth:`load` (the bootstrap does, right after
        construction).
        """
        self._bus = bus
        self._logger = logger or logging.getLogger("libzent.accounts")
        self._accounts_repo = accounts_repo
        self._plugin_manager = plugin_manager

        # never consults it anymore (direct account→plugin assignments).
        self._scheduler = scheduler
        self._settings = settings
        self._audit = audit
        self._client_factory = client_factory
        self._credentials = credentials
        self._limiter = limiter
        self._supervisor_tick = float(supervisor_tick)
        self._api_cache = api_cache_repo
        self._credential_service = credential_service
        self._cooldowns = cooldowns
        self._plugin_state = plugin_state
        #: PHASE 10 (B3): the shared PluginDataStore; enables the
        #: pair+scope-scoped ``data_*`` plugin API
        self._plugin_data = plugin_data
        # typing-indicator RNG (anti-flag: humans trigger typing; disabled
        # by default via the typing_probability account setting = 0.0)
        import random as _random

        self._typing_rng = _random.Random()
        # sleep seam: tests substitute an instant recorder so backoff
        # timing assertions are deterministic (no wall-clock polling —
        # the old shape was the suite's known ~1-in-8 flake)
        self._sleep = asyncio.sleep

        self._accounts: dict[str, Account] = {}
        #: accounts WE attached to the plugin manager (and have not yet
        #: detached) — lets _refresh_plugins tell "plugin manager does not
        #: have the account" (keep the last known view) apart from "attached
        #: but with ZERO plugins" (fail-closed re-attach: the stale view
        #: must be cleared, not kept)
        self._plugin_attached: set[str] = set()
        #: ONE task-ack registry per manager (single bus subscription set)
        self._ack_registry = TaskAckRegistry(bus)
        # FINAL-PHASE (F1-b′): one per-account waiter index per manager —
        # wait_for_message/collect_messages route through it so waiter
        # fan-out is per-account (M/A), not fleet-wide (M×E).
        self._waiter_registry = MessageWaiterRegistry(bus)
        #: (account, plugin) -> services; rebuilt on each activation
        self._services: dict[tuple[str, str], AccountPluginServices] = {}
        #: strong refs for fire-and-forget tasks (documented create_task hazard)
        self._background: set[asyncio.Task[Any]] = set()
        # FINAL-PHASE (F4): single-flight bulk start (a second start_all
        # joins the running sweep) + the sweep's concurrency width
        self._start_all_flight: asyncio.Future[dict[str, Any]] | None = None
        # PHASE-4 (S2 #5): cancelled-but-still-unwinding supervisors —
        # strong refs so they can never be GC'd mid-teardown
        self._dying_supervisors: set[asyncio.Task[None]] = set()
        # PHASE-4 (S4): lifecycle rate governor (None = unlimited)
        self._lifecycle = lifecycle
        # SyncGroupManager (optional): the application-level owner of group
        # membership. When present, every group-scope touch below delegates
        # to it; when absent, the legacy direct limiter plumbing runs.
        self._sync_groups = sync_groups
        # AccountPluginsRepository (optional): direct account→plugin
        # assignments — THE capability source. None (unwired test stacks)
        # behaves like "no assignments": zero plugins, never unrestricted.
        self._account_plugins = account_plugins

        # inbound audit trail (spec §10): every message seen by any account
        bus.subscribe(MessageReceived, self._audit_inbound)

    # ------------------------------------------------------- rate-scope sync
    def _sync_group_scope(self, name: str, group: str | None) -> None:
        """Feed the account's group membership to the limiter (per-account/
        group rate scope). Pure metadata plumbing — NO Discord API is
        involved; tolerant of limiters without group support (tests).

        LEGACY path: only used when no SyncGroupManager is wired (unwired
        test/sim stacks) — the wired path goes through _sync_group_apply.
        """
        register = getattr(self._limiter, "set_account_group", None)
        if register is None:
            return
        try:
            register(name, group)
        except Exception:
            self._logger.exception("group scope registration failed for %s", name)

    async def _sync_group_apply(
        self, name: str, group: str | None, *, forget: bool = False
    ) -> None:
        """One membership -> runtime sync, through the owning manager.

        With SyncGroupManager wired this is the AccountManager ->
        SyncGroupManager -> RateLimitManager path (the wired manager also
        adopts the group row for legacy names). Without it, the legacy
        direct plumbing keeps unwired stacks behaving exactly as before.
        Never raises: group plumbing must not break account lifecycle.
        """
        if self._sync_groups is not None:
            try:
                if forget:
                    outcome = self._sync_groups.forget_account(name)
                else:
                    outcome = self._sync_groups.sync_membership(name, group)
                if inspect.iscoroutine(outcome):
                    await outcome
            except Exception:
                self._logger.exception("sync-group sync failed for account %s", name)
            return
        if forget:
            self._sync_group_scope(name, None)
        else:
            self._sync_group_scope(name, group)

    # ------------------------------------------------------------------ boot
    async def load(self) -> None:
        """Rebuild Account objects (and attach their plugins) from the repo.

        Boot path: awaited by the bootstrap (and the simulation runner)
        right after construction — the constructor itself does no I/O.
        Best-effort: a repository failure must not break the boot.

        Plugin assignments are bulk-loaded FIRST (ONE
        ``account_plugins.all_assignments()`` read feeds every record —
        no per-account queries in this loop), so each
        ``_attach_plugins`` below attaches exactly the persisted set. A
        failed bulk read logs loudly and leaves every account with ZERO
        attached plugins (fail-closed: a repository hiccup must never
        widen capability to "all plugins" — the old no-profile default).

        Group restoration is owned by SyncGroupManager when wired: ONE
        delegated ``sync_runtime()`` pass after the accounts are restored
        (no per-record group logic here). Unwired stacks keep the legacy
        per-record limiter plumbing.
        """
        try:
            records = await self._accounts_repo.list() or []
        except Exception:
            self._logger.exception("could not load accounts from repository")
            return
        assignments = await self._load_assignments()
        bindings = await self._load_bindings()
        for record in records:
            try:
                account = Account(
                    record=record,
                    assigned_plugins=list(assignments.get(record.name, [])),
                    plugin_profiles=dict(bindings.get(record.name, {})),
                )
                self._refresh_credential_source(account)
                self._accounts[record.name] = account
                await self._attach_plugins(record.name)
                if self._sync_groups is None:  # legacy path (see docstring)
                    # workspace pointer first (W3); the v9 sync_group
                    # column is frozen read-only until v11 drops it
                    self._sync_group_scope(
                        record.name,
                        getattr(record, "workspace", None) or record.sync_group,
                    )
            except Exception:
                self._logger.exception("could not restore account %r", record.name)
        if self._sync_groups is not None:
            try:
                outcome = self._sync_groups.sync_runtime()
                if inspect.iscoroutine(outcome):
                    await outcome
            except Exception:
                self._logger.exception("sync-group runtime restore failed")

    # ------------------------------------------------------------ registry
    async def add_account(
        self,
        name: str,
        *,
        token_ref: str | None = None,
        enabled: bool = True,
        sync_group: str | None = None,
        plugins: list[str] | None = None,
        profiles: Any = None,
    ) -> dict[str, Any]:
        """Register + persist an account; returns its status dict.

        Persists to the repository FIRST (a failed write leaves memory
        untouched), then builds the in-memory Account and stores its
        plugin assignments (``plugins`` — the capability source since the
        profile-mediated model was removed). The token VALUE never enters
        the database — only the .env key ref.

        ``profiles`` is deliberately still a named (rejected) parameter so
        a legacy caller gets an explanatory ValueError instead of a bare
        TypeError: profiles no longer control account capabilities.

        Raises:
            AccountError: invalid name (must match NAME_RE), duplicate, or
                a non-list/non-strings ``plugins`` argument.
            ValueError: ``profiles`` was passed (removed architecture).
        """
        if isinstance(name, str) and name.lower() == "all":
            # reserved by 'accounts start all': an account so named could
            # never be started/stopped/filtered individually from the CLI
            raise AccountError("account name 'all' is reserved")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name or ""):
            raise AccountError(
                f"account name {name!r} must match {NAME_RE.pattern} "
                "(lowercase letters/digits/_/-, at most 32 chars)"
            )
        if name in self._accounts:
            raise AccountError(f"account {name!r} already exists")
        if profiles is not None:
            raise ValueError(
                "account creation no longer accepts 'profiles': profiles do "
                "not control capabilities anymore — pass plugins=[...] "
                "(direct account→plugin assignments) instead"
            )
        if plugins is not None and (
            not isinstance(plugins, (list, tuple)) or not all(isinstance(p, str) for p in plugins)
        ):
            raise AccountError("'plugins' must be a list of plugin names")
        record = await self._accounts_repo.add(
            name, token_ref=token_ref, enabled=bool(enabled), sync_group=sync_group
        )
        account = Account(record=record)
        self._refresh_credential_source(account)
        self._accounts[name] = account
        # persist the capability rows, THEN mirror them into the record the
        # attach path reads (a failed write keeps the account fail-closed:
        # zero plugins until an explicit assignment lands)
        persisted = True
        if self._account_plugins is not None:
            try:
                await self._account_plugins.set_plugins(name, list(dict.fromkeys(plugins or [])))
            except Exception:
                persisted = False
                self._logger.exception(
                    "could not persist plugin assignments for new account %r",
                    name,
                )
        # fail-closed: memory must never claim assignments the database
        # lost (they would run this session, then vanish on restart)
        account.assigned_plugins = sorted(set(plugins or [])) if persisted else []
        fresh = await self._repo_get(name)
        if fresh is not None:
            account.record = fresh
        await self._attach_plugins(name)
        await self._sync_group_apply(name, sync_group)
        self._logger.info(
            "account %s added (enabled=%s workspace=%s plugins=%s)",
            name,
            enabled,
            sync_group,
            account.assigned_plugins or "-",
        )
        return account.status_dict()

    def _removal_aborted(self, name: str, account: Account) -> dict[str, Any] | None:
        """Identity guard for the removal cascade (stale-start audit).

        The cascade has many await windows; a same-named account re-added
        during one of them re-occupies the registry slot. Every
        name-keyed destructive step re-verifies that no DIFFERENT Account
        object owns the name before acting (the name is normally ABSENT
        here — we popped it — so only a genuine re-creation trips this).
        """
        current = self._accounts.get(name)
        if current is not None and current is not account:
            self._logger.error(
                "removal of %s aborted: account re-created mid-cascade — "
                "preserving the new account's state",
                name,
            )
            return {"removed": False, "name": name, "recreated": True}
        return None

    async def remove_account(self, name: str) -> dict[str, Any]:
        """Remove a STOPPED account: detach plugins, cancel supervisor, delete.

        Cascades every account-scoped persistent state: the stored
        credential (when a credential service is wired), every cooldown row
        (when a cooldown service is wired), every kv-backed plugin-state
        row (when a plugin state service is wired) and every plugin
        ASSIGNMENT row (when the account-plugins repository is wired) —
        nothing survives for a future same-named account to inherit.

        Raises:
            AccountError: unknown account, or still running/reconnecting
                (stop it first — the refusal happens BEFORE any mutation).
        """
        # pop FIRST: the registry delete is the single atomic act that makes
        # every concurrent start()/stop()/remove() fail with "unknown
        # account" from here on — no check-then-act window can leave a live
        # untracked client behind, and a double remove raises cleanly below
        try:
            account = self._accounts.pop(name)
        except KeyError:
            raise AccountError(f"unknown account {name!r}") from None
        rt = account.runtime
        if rt.status in (
            AccountStatus.RUNNING,
            AccountStatus.RECONNECTING,
            AccountStatus.CONNECTING,
        ):
            # put it back: refusal must not mutate the registry — and must
            # NOT bump the generation either (PHASE-4 fix: the bump used to
            # happen before this check, so a REFUSED removal invalidated
            # the in-flight start, which then stood down as stale and left
            # the account stuck CONNECTING — a live client with no owner)
            self._accounts[name] = account
            raise AccountError(
                f"stop account {name!r} before removing it (status: {rt.status.value})"
            )
        rt.generation += 1  # PHASE-4: this removal invalidates all older ops
        rt.stop_requested = True
        supervisor = rt.supervisor
        rt.supervisor = None
        if supervisor is not None:  # sync context: cancel, the loop reaps it
            supervisor.cancel()
        try:
            # K6 (S1-C7/S5-C3/S10-C3): a FAILED account was never stop()ed,
            # so its plugins may still be ACTIVE (recovery loops,
            # on_load resources). detach fires no lifecycle hooks —
            # deactivate first or they outlive the account's removal.
            #
            # Round-3 (probe_lane_resurrect.py): deactivate ALSO has to run
            # BEFORE the scheduler lane cleanup below (and before the
            # awaited stale-client disconnect). Plugins still active across
            # those awaits kept submitting tasks, and every submit after
            # ``cleanup_account`` re-created the removed account's lane
            # (scheduler ``_ensure_lane``) — a parked worker that leaked
            # until the global stop. With plugins quiet first, the
            # cancel/cleanup pair below is the LAST word on the lanes.
            outcome = self._plugin_manager.deactivate_account(name)
            if inspect.iscoroutine(outcome):  # tolerant: sync pinned, async OK
                await outcome
        except Exception:
            self._logger.exception("plugin deactivation on remove failed for %s", name)
        # Stale-start audit: re-verify identity before every name-keyed
        # destructive step below — the awaits above (and each one that
        # follows) are windows in which a same-named account may have been
        # RE-ADDED, and these steps would otherwise hit the NEW account.
        aborted = self._removal_aborted(name, account)
        if aborted is not None:
            return aborted
        if self._scheduler is not None:
            try:  # drop queued/parked tasks so nothing fires against a
                # re-created same-named account later
                self._scheduler.cancel_matching(account=name)
            except Exception:
                self._logger.exception("task cancel failed for %s", name)
            cleanup = getattr(self._scheduler, "cleanup_account", None)
            if callable(cleanup):
                try:
                    resolved = cleanup(name)  # K1: real method now exists;
                    if inspect.iscoroutine(resolved):  # tolerant of sync fakes
                        await resolved
                except Exception:
                    self._logger.exception("lane cleanup failed for %s", name)
        aborted = self._removal_aborted(name, account)
        if aborted is not None:
            return aborted
        stale_client = rt.client
        rt.client = None
        if stale_client is not None:  # FAILED account never stopped: no leak
            try:
                # bounded like stop()'s 10s disconnect: a hung library
                # close used to park the whole removal cascade forever
                # (registry already popped — every retry hit "unknown")
                await asyncio.wait_for(stale_client.disconnect(), 10.0)
            except Exception:
                self._logger.warning("stale client close failed for %s", name)
                cancel = getattr(stale_client, "_cancel_run_task", None)
                if cancel is not None:
                    with contextlib.suppress(Exception):
                        await cancel()
        # (deactivate already ran BEFORE the lane cleanup above — round-3
        # reorder; the pre-round-3 duplicate deactivate block that lived
        # here was a merge artifact from the isolation audit)
        aborted = self._removal_aborted(name, account)
        if aborted is not None:
            return aborted
        try:
            outcome = self._plugin_manager.detach_account(name)
            if inspect.iscoroutine(outcome):  # tolerant: sync pinned, async OK
                await outcome
        except Exception:
            self._logger.exception("plugin detach failed for %s", name)
        # PHASE-4 (S1-F3): final identity check before the remaining
        # destructive writes below (credential delete, cooldowns, plugin
        # state, mirrors, the repo row itself) — the cascade has had many
        # await windows above, and those writes would destroy the NEW
        # account's data. (The name is normally ABSENT here — we popped
        # it — so only a genuinely different Account object re-occupying
        # the name trips this.)
        aborted = self._removal_aborted(name, account)
        if aborted is not None:
            return aborted
        # the new account (if any) owns the flag now — only clear ours
        self._plugin_attached.discard(name)
        if self._api_cache is not None:
            try:  # persisted DM/user rows are keyed by NAME: a re-created
                await self._api_cache.drop_account(name)  # account must not
            except Exception:  # inherit the old one's DM channel ids
                self._logger.exception("api-cache cascade failed for %s", name)
        self._drop_services(name)
        await self._sync_group_apply(name, None, forget=True)
        if self._credential_service is not None:
            try:
                await self._credential_service.delete(name)
            except Exception:
                self._logger.exception("credential delete failed for %s", name)
        if self._cooldowns is not None:
            try:
                await self._cooldowns.delete_account(name)
            except Exception:
                self._logger.exception("cooldown cascade failed for %s", name)
        if self._plugin_state is not None:
            try:
                await self._plugin_state.cascade_account(name)
            except Exception:
                self._logger.exception("plugin-state cascade failed for %s", name)
        if self._account_plugins is not None:
            try:  # capability rows must not survive into a re-created
                await self._account_plugins.set_plugins(name, [])  # same-named
            except Exception:  # account (it would inherit the grants)
                self._logger.exception("plugin-assignment cascade failed for %s", name)
        # K14/K15: in-memory mirrors and engine residue must not outlive the
        # account either, or a re-created same-named account silently
        # inherits its settings, profile grants, pacing stamps and 429
        # cooldowns. Tolerant lookups keep partial test doubles working
        # (Any-typed locals: the doubles are a genuine dynamic boundary).
        settings_mgr: Any = getattr(self, "_settings", None)
        if hasattr(settings_mgr, "forget_account"):
            try:
                settings_mgr.forget_account(name)
            except Exception:
                self._logger.exception("settings mirror cascade failed for %s", name)

        limiter: Any = getattr(self, "_limiter", None)
        if hasattr(limiter, "prune_account"):
            try:
                resolved = limiter.prune_account(name)
                if inspect.iscoroutine(resolved):
                    await resolved
            except Exception:
                self._logger.exception("rate-engine residue cascade failed for %s", name)
        if self._lifecycle is not None:  # PHASE-4 (S4): pacer cleanup
            try:
                self._lifecycle.forget(name)
            except Exception:
                self._logger.exception("lifecycle pacer cascade failed for %s", name)
        if self._waiter_registry is not None:  # FINAL-PHASE (F1): waiters
            try:
                self._waiter_registry.forget_account(name)
            except Exception:
                self._logger.exception("waiter registry cascade failed for %s", name)
        _forget_account_loggers(name)  # FINAL-PHASE (G3/R4): loggerDict
        # PHASE-4 (S1-F3): final identity check before the irreversible DB
        # row delete — the cascade has had many await windows above.
        current = self._accounts.get(name)
        if current is not None and current is not account:
            self._logger.error(
                "removal of %s aborted before DB delete: account re-created mid-cascade", name
            )
            return {"removed": False, "name": name, "recreated": True}
        existed = bool(await self._accounts_repo.delete(name))
        self._logger.info("account %s removed (existed=%s)", name, existed)
        return {"removed": name, "existed": existed}

    def get(self, name: str) -> Account:
        """The Account object (AccountError when unknown / unhashable name)."""
        return self._get(name)

    def names(self) -> list[str]:
        """Sorted account names."""
        return sorted(self._accounts)

    async def accounts(self, *, refresh_records: bool = False) -> list[dict[str, Any]]:
        """Status dicts of every account, sorted by name.

        FINAL-PHASE (F3): memory-first by default — the runtime record,
        plugin view and credential source are all written at mutation
        time, so the 1 Hz dashboard poll no longer performs 2 DB reads
        per account per second. ``refresh_records=True`` (or the 30 s
        :meth:`reconcile_records` pass) re-reads the authoritative rows,
        bounding externally-edited state (a second process, manual
        sqlite) instead of polling for it.
        """
        out: list[dict[str, Any]] = []
        for name in sorted(self._accounts):
            account = self._accounts[name]
            if refresh_records:
                await self._refresh_record(account)
            self._refresh_plugins(account)
            self._refresh_credential_source(account)
            out.append(account.status_dict())
        return out

    async def reconcile_records(self) -> None:
        """Low-frequency authoritative re-read of every account row.

        Bounded staleness for out-of-band DB edits (external writers);
        in-process mutations never need this — they update the memory
        record at write time.
        """
        for account in list(self._accounts.values()):
            try:
                await self._refresh_record(account)
            except Exception:
                self._logger.exception("record reconciliation failed for %s", account.record.name)

    async def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """Flip the persisted enabled flag (False = skipped by start_all)."""
        account = self._get(name)
        await self._accounts_repo.set_enabled(name, bool(enabled))  # persist first
        account.record.enabled = bool(enabled)
        return account.status_dict()

    # ---------------------------------------------------- plugin assignments
    async def assigned_plugins(self, name: str) -> list[str]:
        """Sorted plugin names assigned to the account (capability read).

        Memory-first: answers from the in-memory mirror every mutation
        rewrites, so the 1 Hz status poll never queries the repository.
        """
        return sorted(set(self._get(name).assigned_plugins or []))

    async def assign_plugin(self, name: str, plugin: str) -> None:
        """Assign ONE plugin to an account (idempotent; re-attaches NOW).

        Repository first, then the in-memory mirror, then
        ``_attach_plugins`` — a running account's plugin set follows the
        assignment immediately, not on its next start.

        Raises:
            AccountError: unknown account.
            RuntimeError: no account-plugins repository is wired.
        """
        self._get(name)
        self._require_account_plugins()
        await self._account_plugins.assign_plugin(name, plugin)
        await self._refresh_assignment(name)
        await self._attach_plugins(name)
        self._logger.info("plugin %r assigned to account %r", plugin, name)

    async def unassign_plugin(self, name: str, plugin: str) -> bool:
        """Remove ONE plugin assignment (idempotent; re-attaches NOW).

        Returns whether the plugin WAS assigned (pre-state via the
        repository, like the profile unassign contract) — removing a
        missing assignment is not an error.

        Raises:
            AccountError: unknown account.
            RuntimeError: no account-plugins repository is wired.
        """
        self._get(name)
        self._require_account_plugins()
        existed = plugin in set(await self._plugins_of(name))
        await self._account_plugins.unassign_plugin(name, plugin)
        await self._refresh_assignment(name)
        await self._attach_plugins(name)
        self._logger.info(
            "plugin %r unassigned from account %r (was assigned: %s)",
            plugin,
            name,
            existed,
        )
        return existed

    async def set_account_plugins(self, name: str, plugins: list[str]) -> list[str]:
        """REPLACE the account's whole assignment set (re-attaches NOW).

        Returns the stored sorted list (canonical form on disk). The
        repository's delete-then-insert is one transaction, so concurrent
        readers never observe a half-replaced set.

        Raises:
            AccountError: unknown account.
            RuntimeError: no account-plugins repository is wired.
        """
        self._get(name)
        self._require_account_plugins()
        stored = list(await self._account_plugins.set_plugins(name, plugins or []))
        await self._refresh_assignment(name)
        await self._attach_plugins(name)
        self._logger.info("account %s plugin set replaced (%d)", name, len(stored))
        return stored

    def _require_account_plugins(self) -> None:
        """Honest failure on stacks built without the assignment repo."""
        if self._account_plugins is None:
            raise RuntimeError(
                "account plugin assignments are not configured on this "
                "account manager (db.account_plugins was not wired)"
            )

    async def _plugins_of(self, name: str) -> list[str]:
        """Sorted assignments of one account from the repository.

        FAIL-CLOSED: a repository failure reads as "no assignments"
        (never as unrestricted), loudly logged.
        """
        if self._account_plugins is None:
            return []
        try:
            return sorted(
                {
                    p
                    for p in (await self._account_plugins.plugins_of(name) or [])
                    if isinstance(p, str)
                }
            )
        except Exception:
            self._logger.exception(
                "could not read plugin assignments for %r (fail-closed: none)",
                name,
            )
            return []

    async def _refresh_assignment(self, name: str) -> list[str]:
        """Repository → in-memory mirror for one account's assignments.

        Runs after EVERY assignment mutation so ``Account.assigned_plugins``
        stays authoritative for both the attach path and the status poll.
        The profile-binding mirror refreshes in the same pass: a
        replacement/unassign DELETES rows (taking their ``profile``
        column with them), so the binding view must never outlive the
        assignment it belonged to.
        """
        account = self._accounts.get(name)
        if account is None:
            return []
        stored = await self._plugins_of(name)
        account.assigned_plugins = stored
        await self._refresh_bindings(name)
        return stored

    async def _load_assignments(self) -> dict[str, list[str]]:
        """ONE bulk boot read of every account's assignments.

        Accounts with zero plugins are omitted by the repository — callers
        must ``.get(name, [])``. A failure logs loudly and returns {} (every
        account then boots fail-closed with zero attached plugins, never
        unrestricted).
        """
        if self._account_plugins is None:
            return {}
        try:
            return dict(await self._account_plugins.all_assignments() or {})
        except Exception:
            self._logger.exception(
                "could not load account plugin assignments (fail-closed: "
                "accounts boot with zero plugins until the repository "
                "recovers and they are restarted)"
            )
            return {}

    async def _load_bindings(self) -> dict[str, dict[str, str | None]]:
        """ONE bulk boot read of every account's profile bindings.

        Same conventions as :meth:`_load_assignments` (omitted-empty,
        fail-soft {} on a repository failure — a binding lost to a read
        hiccup reads as "plugin defaults" and is re-written on the next
        binding mutation; profiles never gate CAPABILITY, only settings).
        """
        if self._account_plugins is None:
            return {}
        reader = getattr(self._account_plugins, "all_bindings", None)
        if not callable(reader):  # older stub doubles: no bindings column
            return {}
        try:
            return dict(await reader() or {})
        except Exception:
            self._logger.exception(
                "could not load account plugin profile bindings "
                "(accounts boot with plugin defaults until rebound)"
            )
            return {}

    async def _refresh_bindings(self, name: str) -> None:
        """Repository → in-memory mirror for one account's bindings."""
        account = self._accounts.get(name)
        if account is None or self._account_plugins is None:
            return
        reader = getattr(self._account_plugins, "bindings", None)
        if not callable(reader):
            return
        try:
            account.plugin_profiles = dict(await self._account_plugins.bindings(name) or {})
        except Exception:
            self._logger.exception("could not refresh plugin profile bindings for %r", name)

    # --------------------------------------------------------- profile views
    async def plugin_profiles(self, name: str) -> dict[str, str | None]:
        """``{plugin: profile name or None}`` for every assigned plugin.

        Memory-first (the in-memory mirror the boot load and every
        assignment/binding mutation rewrite), so the status poll and the
        profile picker never query the repository per read.
        """
        account = self._get(name)
        return {
            plugin: account.plugin_profiles.get(plugin)
            for plugin in sorted(set(account.assigned_plugins or []))
        }

    def channels_snapshot(self, name: str) -> list[dict[str, Any]]:
        """Text-channel snapshot of the account's LIVE client.

        [] for offline/unstarted accounts — never raises (see
        :meth:`Account.channels_snapshot`). Unknown accounts raise
        AccountError like every name-addressed accessor.
        """
        return self._get(name).channels_snapshot()

    # ------------------------------------------------------------- lifecycle
    async def start(self, name: str) -> dict[str, Any]:
        """Connect one account, activate its plugins, spawn the supervisor.

        Raises:
            CredentialsError: the account's token ref is missing from .env.
            AccountError: already running/connecting/reconnecting, the client
                factory failed, or connect() failed (status becomes ``failed``
                and ``AccountFailed`` is published in every failure case).
        """
        account = self._get(name)
        rt = account.runtime
        if rt.status in (
            AccountStatus.RUNNING,
            AccountStatus.CONNECTING,
            AccountStatus.RECONNECTING,
        ):
            raise AccountError(f"account {name!r} is already {rt.status.value}")
        self._lifecycle_gate("start", name, rt.status)
        rt.stop_requested = False
        rt.lost.clear()
        rt.last_error = None
        rt.generation += 1  # PHASE-4: this start invalidates all older ops
        gen = rt.generation
        rt.status = AccountStatus.CONNECTING
        try:
            await self._connect_client(name, rt)
        except asyncio.CancelledError:
            if rt.stop_requested:
                # a stop()/restart raced the start: not a failure
                rt.status = AccountStatus.STOPPED
                self._discard_half_built_client(name, rt)
                raise
            if rt.generation != gen:
                # Stale-start audit: only stop/estop/remove set
                # stop_requested — a bare generation bump here means a
                # NEWER START owns the account (a completed stop + restart
                # raced this one). Its teardown already released our
                # client; never touch the newer owner's rt state.
                raise
            rt.status = AccountStatus.FAILED
            rt.last_error = "start cancelled"
            raise
        except (KeyboardInterrupt, SystemExit):
            raise  # process-level signals are never swallowed here
        except _StaleGeneration:
            # A newer lifecycle operation owns this account now (stop,
            # restart, remove — or a newer START). Abort quietly: no status
            # write, no AccountFailed, no classification — never fight the
            # owner. Teardown split (stale-start audit): only a stop-shaped
            # owner (stop_requested set) may discard rt.client; when a
            # newer START owns the account, rt.client/status are ITS state
            # — and _connect_client already disconnected OUR client before
            # raising, so there is nothing left to release either way.
            if rt.stop_requested:
                self._discard_half_built_client(name, rt)
            self._logger.info("start of %s superseded by newer lifecycle operation", name)
            return account.status_dict()
        except Exception as exc:
            if rt.stop_requested:
                # stop() completed while connect() was parked: its teardown
                # already ran (rt.client was None then). This is a benign
                # race — NOT an account failure: leave STOPPED and never
                # publish AccountFailed after AccountDisconnected.
                rt.status = AccountStatus.STOPPED
                self._discard_half_built_client(name, rt)
                self._logger.info("account %s start aborted by concurrent stop", name)
                raise AccountError(f"account {name!r} start cancelled by stop") from exc
            if rt.generation != gen:
                # Stale-start audit: a NEWER START owns the account — never
                # clobber its status/client/plugins with this (older)
                # start's failure. rt.client either already went through
                # the newer owner's teardown or belongs to it.
                self._logger.info("start of %s superseded by newer lifecycle operation", name)
                raise AccountError(
                    f"account {name!r} start superseded by a newer lifecycle operation"
                ) from exc
            self._start_failed(name, rt, exc)
            if isinstance(exc, (AccountError, CredentialsError)):
                raise
            raise AccountError(f"account {name!r} failed to connect: {exc}") from exc
        if rt.stop_requested:
            # K2 (S1-C1/S10-C1): the racing stop() may have completed BEFORE
            # rt.client was assigned (e.g. while we awaited token resolution),
            # so its disconnect never saw this client. Disconnect HERE or the
            # gateway stays live forever on a "stopped" account whose STOPPED
            # guard then blocks every future cleanup (zombie self-bot).
            client = rt.client
            rt.client = None  # callbacks go stale immediately
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    self._logger.exception("abort-disconnect failed for %s", name)
            rt.status = AccountStatus.STOPPED
            self._logger.info("account %s start aborted by concurrent stop", name)
            return account.status_dict()
        if rt.generation != gen:
            # Stale-start audit: the generation bump came from a NEWER
            # START (only stop/estop/remove set stop_requested, and they
            # were ruled out above). rt.client/status/plugins belong to
            # that newer owner now — leave them untouched.
            self._log_superseded_start(name)
            return account.status_dict()
        rt.status = AccountStatus.RUNNING
        rt.started_at = time.monotonic()
        rt.session_started = rt.started_at  # M2: flap window starts at READY
        self._refresh_credential_source(account)
        self._logger.info("account %s running (user_id=%s)", name, rt.user_id)
        # plugins: re-attach with the CURRENT profile grants (assignments
        # take effect on the next start — ported semantics), then activate
        attach_activated = await self._attach_plugins(name)
        if self._stop_landed(rt):
            # PHASE-4 (S1-F1): stop landed during the attach await — the
            # NEW runtimes must not be activated on a stopped account.
            self._abort_stale_start(name, rt)
            return account.status_dict()
        if rt.generation != gen:
            # Stale-start audit: a newer START owns the account (its own
            # attach/activate already ran). Stand down log-only — a
            # full teardown would deactivate/drop the NEWER start's
            # plugins, services and client.
            self._log_superseded_start(name)
            return account.status_dict()
        if attach_activated:
            # _attach_plugins already activated the fresh set (RUNNING
            # account) — activating again would fire hooks twice
            pass
        else:
            try:
                outcome = self._plugin_manager.activate_account(name, self._services_factory(name))
                if inspect.iscoroutine(outcome):  # pinned sync; async tolerated
                    await outcome
            except Exception:
                self._logger.exception(
                    "plugin activation failed for %s (account keeps running)", name
                )
        if self._stop_landed(rt):
            # PHASE-4 (S1-F1): stop() raced the ACTIVATION await. Without
            # this, freshly activated plugins (their background loops!)
            # keep running on a stopped, supervisor-less account. Clean up
            # everything this start created: hooks, services, client.
            self._abort_stale_start(name, rt)
            return account.status_dict()
        if rt.generation != gen:
            # Stale-start audit: a newer START activated its own plugin set
            # on this account while we were parked — deactivating or
            # tearing anything down here would kill THE NEWER start's
            # runtime. Stand down log-only.
            self._log_superseded_start(name)
            return account.status_dict()
        self._bus.publish_nowait(AccountConnected(account=name))
        self._lifecycle_commit("start", name, ok=True)
        if self._stop_landed(rt):
            self._abort_stale_start(name, rt)
            return account.status_dict()
        if rt.generation != gen:
            # ownership changed between the publish and the spawn: the
            # newer owner spawns its own supervisor — never spawn a second
            self._log_superseded_start(name)
            return account.status_dict()
        # K5 (S1-C4) + stale-start audit: clear any surviving prior-
        # generation supervisor UNCONDITIONALLY. With reconnect disabled
        # the spawn below is skipped, and a live old supervisor would
        # wedge the account RECONNECTING on the next drop (_on_disconnect
        # takes its branch because rt.supervisor is set, but the old
        # driver then exits on its generation check — nobody reconnects).
        old = rt.supervisor
        rt.supervisor = None
        if old is not None and not old.done():
            old.cancel()
        if self._reconnect_settings(name)["enabled"]:
            rt.supervisor = asyncio.create_task(
                self._supervise(name, rt),
                name=f"supervise-{name}#g{gen}",
            )

            # PHASE-4 (S2 #4): an unexpected supervisor death must never
            # wedge the account in RECONNECTING forever — log it and settle
            # FAILED so automation/start() can recover.
            def _on_supervisor_done(
                task: asyncio.Task[None], _name: str = name, _rt: AccountRuntime = rt
            ) -> None:
                self._supervisor_done(_name, _rt, task)

            rt.supervisor.add_done_callback(_on_supervisor_done)
        return account.status_dict()

    def _stop_landed(self, rt: AccountRuntime) -> bool:
        """Live read of ``stop_requested`` WITHOUT mypy attribute-narrowing.

        The flag is mutated by OTHER tasks (stop/estop/remove) while this
        task is parked on an await, so a check that mypy narrowed earlier
        in the flow stays a live re-read here — calling through a method
        keeps every post-await check reachable to the checker instead of
        being mis-flagged as dead code."""
        return bool(rt.stop_requested)

    def _supervisor_done(self, name: str, rt: AccountRuntime, task: asyncio.Task[None]) -> None:
        """Done-callback for supervisor tasks: surface unexpected deaths."""
        if task.cancelled():
            return  # deliberate stop/removal — the normal path
        exc = task.exception()
        if exc is None:
            return  # clean stand-down
        self._logger.error("supervisor for %s died unexpectedly: %r", name, exc)
        if rt.supervisor is task and not rt.stop_requested:
            rt.status = AccountStatus.FAILED
            rt.last_error = f"supervisor died: {exc!r}"
            rt.lost.clear()
            if rt.supervisor is task:
                rt.supervisor = None

    # ------------------------------------------------- lifecycle governor
    def _lifecycle_gate(self, op: str, name: str, status: AccountStatus) -> None:
        """PHASE-4 (S4): consult the lifecycle governor; raise when refused.

        Called only AFTER the state gates — state-illegal operations never
        reach the governor and never consume lifecycle capacity.
        """
        governor = self._lifecycle
        if governor is None:
            return
        try:
            decision = governor.precheck(name, op, status=status)
        except Exception:
            self._logger.exception("lifecycle precheck failed for %s", name)
            return  # the limiter must never break the operation
        if decision.allowed:
            return
        self._logger.warning(
            "account %s %s refused by lifecycle governor (%s): retry in %.1fs",
            name,
            op,
            decision.reason,
            decision.retry_after_s,
        )
        raise AccountError(
            f"account {name!r} {op} rate-limited ({decision.reason}): "
            f"retry in {decision.retry_after_s:.1f}s"
        )

    def _lifecycle_commit(
        self, op: str, name: str, *, ok: bool, invalid_credential: bool = False
    ) -> None:
        governor = self._lifecycle
        if governor is None:
            return
        try:
            governor.commit(name, op, ok=ok, invalid_credential=invalid_credential)
        except Exception:
            self._logger.exception("lifecycle commit failed for %s", name)

    def lifecycle_stats(self) -> dict[str, Any]:
        """Governor stats for status surfaces ({} when unlimited)."""
        return self._lifecycle.stats() if self._lifecycle is not None else {}

    def _log_superseded_start(self, name: str) -> None:
        """Log-only stand-down for a start superseded by a NEWER start.

        Stale-start audit: when the generation bump came from a newer
        START (stop_requested stays False — only stop/estop/remove set it),
        rt.client, rt.status, the plugin activations and the services
        facades all belong to that newer owner. The stale start must not
        touch any of them; its own half-built client was already released
        by ``_connect_client``'s stale path before it raised.
        """
        self._logger.info(
            "start of %s superseded by a newer start; leaving the newer "
            "owner's client/status/plugins untouched",
            name,
        )

    def _abort_stale_start(self, name: str, rt: AccountRuntime) -> None:
        """PHASE-4 cleanup for a start() that lost ownership mid-flight.

        Deactivates any plugins the stale start activated (S1-F1: they
        would otherwise run invisible to lifecycle control), drops the
        services facades it created, discards its client, and settles
        STOPPED — without publishing lifecycle events a second time.
        STOP-shaped supersession only (caller verified stop_requested).
        """
        rt.status = AccountStatus.STOPPED
        try:
            outcome = self._plugin_manager.deactivate_account(name)
            if asyncio.iscoroutine(outcome):
                task = asyncio.get_running_loop().create_task(
                    outcome, name=f"stale-start-deactivate-{name}"
                )
                self._background.add(task)
                task.add_done_callback(self._reap_background)
        except Exception:
            self._logger.exception("stale-start plugin cleanup failed for %s", name)
        self._drop_services(name)
        self._discard_half_built_client(name, rt)
        self._logger.info("account %s start aborted by concurrent lifecycle change", name)

    def _discard_half_built_client(self, name: str, rt: AccountRuntime) -> None:
        """Drop a client a concurrent stop() never saw (start-race cleanup).

        Synchronous best-effort: the client's disconnect is scheduled (or
        called when it exposes a sync close) so a gateway connected during
        the race window can never linger invisible to every future stop().
        """
        client = rt.client
        rt.client = None
        if client is None:
            return
        try:
            outcome = client.disconnect()
            if inspect.iscoroutine(outcome):
                task = asyncio.get_running_loop().create_task(
                    outcome, name=f"discard-client-{name}"
                )
                self._background.add(task)
                task.add_done_callback(self._reap_background)
        except Exception:
            self._logger.warning("half-built client cleanup failed for %s", name)

    def _reap_background(self, task: asyncio.Task[Any]) -> None:
        """Drop one finished fire-and-forget task; surface its failure.

        Without the exception read here, a background task that dies (e.g.
        an estop whose account was removed before the stop ran) vanishes
        with nothing but a GC-time "exception was never retrieved" warning.
        """
        self._background.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._logger.error("background task %s failed: %s", task.get_name(), exc)

    async def stop(self, name: str, *, _bypass_lifecycle: bool = False) -> dict[str, Any]:
        """Stop one account (idempotent; plugins deactivated first).

        Order (pinned): stop_requested=True, deactivate plugins (hooks run
        while the cache is still available), ``await client.disconnect()``
        (None-safe), reap the supervisor (bounded 1s wait, then cancel),
        status stopped, publish ``AccountDisconnected(reason="stopped")``.

        Single-flight: concurrent stop() calls (a background estop task +
        a dashboard stop, stop_all racing a user stop) join the in-flight
        stop instead of re-running the teardown — the disconnect event and
        plugin hooks fire exactly once.
        """
        account = self._get(name)
        rt = account.runtime
        async with rt.stop_lock:
            if rt.status == AccountStatus.STOPPED and rt.client is None and rt.supervisor is None:
                return account.status_dict()  # idempotent: nothing re-fires
            # STOPPED with a lingering client/supervisor (a start-race
            # survivor) falls THROUGH to the full teardown below
            if not _bypass_lifecycle:
                self._lifecycle_gate("stop", name, rt.status)
            was_live = (
                rt.status
                in (
                    AccountStatus.RUNNING,
                    AccountStatus.RECONNECTING,
                    AccountStatus.CONNECTING,
                )
                or rt.client is not None
            )
            rt.stop_requested = True
            rt.generation += 1  # PHASE-4: this stop invalidates older ops
            gen = rt.generation
            rt.lost.set()  # wake the supervisor so it sees stop_requested NOW
            try:
                outcome = self._plugin_manager.deactivate_account(name)
                if inspect.iscoroutine(outcome):  # pinned sync; async tolerated
                    # FINAL-PHASE (F4): a hung plugin hook must not wedge a
                    # fleet halt — bounded, then continue the teardown.
                    # Stale-start audit: capture the shielded coroutine as a
                    # TRACKED background task BEFORE awaiting — a timeout
                    # cancels only the shield, and an unreferenced inner
                    # task would orphan (its exception never retrieved).
                    inner = asyncio.ensure_future(outcome)
                    self._background.add(inner)
                    inner.add_done_callback(self._reap_background)
                    await asyncio.wait_for(asyncio.shield(inner), timeout=5.0)
            except asyncio.TimeoutError:
                self._logger.error(
                    "plugin deactivation timed out for %s (continuing teardown)", name
                )
            except Exception:
                self._logger.exception("plugin deactivation failed for %s", name)
            client = rt.client
            rt.client = None  # callbacks go stale from here on (see _wire_client)
            if client is not None:
                try:
                    # FINAL-PHASE (F4): bounded disconnect — a hung library
                    # close must not hold a stop_all semaphore slot forever.
                    # Stale-start audit: track the shield's inner task so a
                    # timeout never orphans it (see deactivate above).
                    disconnecting = asyncio.ensure_future(client.disconnect())
                    self._background.add(disconnecting)
                    disconnecting.add_done_callback(self._reap_background)
                    await asyncio.wait_for(asyncio.shield(disconnecting), timeout=10.0)
                except asyncio.TimeoutError:
                    self._logger.error("client disconnect timed out for %s (force-reaping)", name)
                    force = getattr(client, "_cancel_run_task", None)
                    if callable(force):
                        with contextlib.suppress(Exception):
                            await force()
                except Exception:
                    self._logger.exception("client disconnect failed for %s", name)
            supervisor = rt.supervisor
            rt.supervisor = None
            if supervisor is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(supervisor), timeout=1.0)
                except asyncio.TimeoutError:
                    supervisor.cancel()
                    await asyncio.wait({supervisor}, timeout=1.0)  # bounded reap
                    if not supervisor.done():
                        # PHASE-4 (S2 #5): the cancelled supervisor is still
                        # unwinding (e.g. a library close that swallows
                        # cancellation). Keep a strong reference so it can
                        # never be GC'd mid-unwind and stays observable.
                        self._dying_supervisors.add(supervisor)
                        supervisor.add_done_callback(self._dying_supervisors.discard)
                        self._logger.warning(
                            "supervisor for %s still unwinding after cancel (tracked: %s)",
                            name,
                            supervisor.get_name(),
                        )
                except asyncio.CancelledError:
                    supervisor.cancel()  # caller cancelled us — don't leak the task
                    raise
                except Exception:
                    # a CRASHED supervisor must not abort the teardown —
                    # stop() still has to reach its STOPPED terminal state
                    self._logger.exception("supervisor of %s crashed during reap", name)
            if rt.generation != gen:
                # PHASE-4 (S1-F6): a NEWER start() took ownership while this
                # stop was parked in an await. Its CONNECTING/RUNNING state
                # is authoritative — never clobber it with our terminal
                # writes. The teardown above (client disconnect, supervisor
                # reap, plugin deactivation) still ran for THIS generation's
                # resources, which is exactly what we owed it.
                self._logger.info(
                    "stop of %s superseded by a newer start; leaving its state untouched", name
                )
                return account.status_dict()
            rt.status = AccountStatus.STOPPED
            rt.lost.clear()
            self._drop_services(name)
            if not _bypass_lifecycle:
                # estop/teardown stops don't stamp the cooldown clock:
                # restarting right after an emergency stop is the RECOVERY
                # flow, not operator churn (PHASE-4 S4 hardening).
                self._lifecycle_commit("stop", name, ok=True)
            if was_live:
                self._bus.publish_nowait(AccountDisconnected(account=name, reason="stopped"))
            self._logger.info("account %s stopped", name)
            return account.status_dict()

    async def start_all(self, *, stagger: float = 5.0) -> dict[str, Any]:
        """Start every ENABLED account through the bounded lifecycle scheduler.

        FINAL-PHASE (F4) semantics:
        * BOUNDED CONCURRENCY (``_START_ALL_WIDTH`` lanes): one hung
          connect (90s worst case) delays only its own lane — the fleet
          keeps progressing (the old sequential sweep blocked on every
          straggler).
        * ADMISSION PACING: one start admitted per ``stagger`` seconds
          (monotonic admission clock, no per-account sleep) — the 5s
          default equals the IdentifyGate interval, so the global
          lifecycle bucket never refuses a paced sweep.
        * OVERTAKING: a newer stop/estop/remove (generation bump) on an
          account whose start has not yet been admitted CANCELS that
          pending start — the operator's decision always wins.
        * SINGLE-FLIGHT: a second concurrent start_all JOINS the running
          sweep and returns its report with ``"joined": true``.
        * Failures are recorded per account and never abort the sweep.
        """
        if self._start_all_flight is not None:
            report = await asyncio.shield(self._start_all_flight)
            report = dict(report)
            report["joined"] = True
            return report
        flight = asyncio.get_running_loop().create_future()
        self._start_all_flight = flight
        started: list[str] = []
        failed: dict[str, str] = {}
        skipped: list[str] = []
        overtaken: list[str] = []
        try:
            # deterministic snapshot with each account's generation: a
            # queued start whose generation moves (stop/estop/remove) is
            # cancelled instead of executed
            pending = []
            for name in sorted(self._accounts):
                account = self._accounts[name]
                if not account.record.enabled:
                    skipped.append(name)
                    self._logger.info("skipping disabled account %s", name)
                    continue
                pending.append((name, account.runtime.generation))
            semaphore = asyncio.Semaphore(_START_ALL_WIDTH)
            admission = [0.0]  # next-admit monotonic; list = closure cell

            async def _start_one(name: str, snap_gen: int) -> None:
                account = self._accounts.get(name)
                if account is None or account.runtime.generation != snap_gen:
                    overtaken.append(name)  # stop/estop/remove won the race
                    return
                # admission pacing: RESERVE the slot synchronously first,
                # then sleep to it. Computing the deadline after an await
                # collapsed every sleeper onto the same slot (they all
                # woke at once and proceeded together), defeating the
                # stagger — and exhausting the governor's start bucket on
                # mid-size fleets.
                now = time.monotonic()
                slot = max(admission[0], now)
                admission[0] = slot + (stagger if stagger > 0 else 0.0)
                if slot > now:
                    await asyncio.sleep(slot - now)
                account = self._accounts.get(name)
                if account is None or account.runtime.generation != snap_gen:
                    overtaken.append(name)  # a stop landed during the wait
                    return
                async with semaphore:
                    try:
                        await self.start(name)
                        started.append(name)
                    except Exception as exc:
                        failed[name] = f"{type(exc).__name__}: {exc}"
                        self._logger.exception("failed to start account %s", name)

            if pending:
                await asyncio.gather(*[asyncio.create_task(_start_one(n, g)) for n, g in pending])
            report = {
                "started": sorted(started),
                "failed": failed,
                "skipped": skipped,
                "overtaken": sorted(overtaken),
            }
            if not flight.done():
                flight.set_result(report)
            return report
        finally:
            # Stale-start audit: a CANCELLED sweep must resolve the flight —
            # joiners are parked on `await asyncio.shield(flight)` and would
            # hang forever on an unresolved future. Cancelling it propagates
            # the cancellation to every joiner (shield passes an INNER
            # cancellation through to the outer await).
            if not flight.done():
                flight.cancel()
            self._start_all_flight = None

    async def stop_all(self) -> dict[str, Any]:
        """Graceful shutdown of every account; logs and continues per account.

        Accounts stop CONCURRENTLY (bounded): a fleet-wide halt is a
        safety-critical latency — sequential per-account disconnects could
        outlive a dashboard estop's 10s route budget.
        """
        stopped: list[str] = []
        failed: dict[str, str] = {}

        async def _stop_one(name: str) -> None:
            try:
                await self.stop(name, _bypass_lifecycle=True)  # PHASE-4:
                # fleet teardown must never be blocked by the governor
                stopped.append(name)
            except Exception as exc:
                failed[name] = f"{type(exc).__name__}: {exc}"
                self._logger.exception("failed to stop account %s", name)

        names = sorted(self._accounts)
        if not names:
            return {"stopped": stopped, "failed": failed}
        semaphore = asyncio.Semaphore(8)

        async def _limited(name: str) -> None:
            async with semaphore:
                await _stop_one(name)

        await asyncio.gather(*(_limited(n) for n in names))
        return {"stopped": stopped, "failed": failed}

    def emergency_stop(self, name: str) -> dict[str, Any]:
        """SYNCHRONOUS account-scoped emergency stop: returns immediately.

        Semantics (documented per the pinned contract): cancel every queued/
        running task of this account (``scheduler.cancel_matching``), flip
        ``stop_requested`` (so no reconnect can race us) and fire the full
        :meth:`stop` as a background task — the account disconnects without
        the caller awaiting anything. The disconnect/teardown completes
        asynchronously; poll ``get(name).runtime.status`` or watch
        ``AccountDisconnected`` for completion. Requires a running event loop.

        Raises:
            AccountError: unknown account or no running event loop.
        """
        account = self._get(name)
        rt = account.runtime
        rt.stop_requested = True  # immediate: blocks any reconnect flip
        rt.generation += 1  # PHASE-4: estop invalidates older ops
        rt.lost.set()  # wake the supervisor NOW
        cancelled = 0
        try:
            cancelled = int(self._scheduler.cancel_matching(account=name) or 0)
        except Exception:
            self._logger.exception(
                "scheduler cancel_matching failed during emergency stop of %s", name
            )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise AccountError(
                "emergency_stop(...) needs a running event loop (call it from async context)"
            ) from exc
        task = loop.create_task(self.stop(name, _bypass_lifecycle=True), name=f"estop-{name}")
        self._background.add(task)
        task.add_done_callback(self._reap_background)
        self._logger.error("EMERGENCY STOP %s: cancelled %d tasks, disconnecting", name, cancelled)
        return {"account": name, "cancelled_tasks": cancelled, "stop_initiated": True}

    # ------------------------------------------------------------ supervisor
    async def _supervise(self, name: str, rt: AccountRuntime) -> None:
        """Watch one account; reconnect with backoff after unexpected drops.

        Polls the ``lost`` event / status every ``supervisor_tick`` seconds
        (ported cadence). Deliberate stops set ``stop_requested`` and the
        supervisor stands down without reconnecting. Cancellation (stop with
        a stuck backoff sleep) propagates — the reaper in stop() handles it.

        PHASE-4 ownership: the supervisor captures its lifecycle generation
        AND its own task identity at entry. A restart bumps the generation
        (and reassigns ``rt.supervisor``), so an old supervisor that wakes
        late exits instead of reconnecting a newer generation's account —
        the dual-supervisor client ping-pong class is closed by identity,
        not just by the K5 cancel.
        """
        gen = rt.generation
        me = asyncio.current_task()
        try:
            while not rt.stop_requested and rt.generation == gen:
                if name not in self._accounts:  # removed mid-flight
                    return
                if rt.supervisor is not me:
                    return  # a newer owner spawned its own supervisor
                await asyncio.sleep(0)  # always yield: the reconnect-disabled
                # path below can return without awaiting, and a fully
                # synchronous while body would starve the entire event loop
                if rt.status == AccountStatus.RECONNECTING or rt.lost.is_set():
                    try:
                        await self._reconnect_loop(name, rt, gen=gen)
                    except asyncio.CancelledError:
                        raise  # deliberate stop/removal — never swallow
                    except Exception as exc:
                        # Round-3 (backoff-overflow zombie): an unexpected
                        # crash in the reconnect loop previously killed the
                        # supervisor SILENTLY — the account then sat in
                        # RECONNECTING forever with nobody driving it. Stand
                        # down as FAILED (start() stays usable for
                        # automation) and surface the crash in last_error.
                        if not rt.stop_requested:
                            rt.status = AccountStatus.FAILED
                            rt.last_error = (
                                f"reconnect supervisor crashed: {type(exc).__name__}: {exc}"
                            )
                            rt.lost.clear()
                        self._logger.exception(
                            "reconnect supervisor for %s crashed; standing down (status %s)",
                            name,
                            rt.status.value,
                        )
                        return
                    continue
                # PHASE-4 (S7-C8): event-driven wait with a long backstop —
                # the 0.2s poll was 5 timer+task wakeups/s per idle account.
                # Every real trigger sets ``lost`` (drop, stop, estop); the
                # 60s backstop only covers pathological lost-wake cases.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(rt.lost.wait(), timeout=60.0)
        except asyncio.CancelledError:
            raise  # deliberate stop/removal — stand down, never swallow

    async def _reconnect_loop(
        self, name: str, rt: AccountRuntime, *, gen: int | None = None
    ) -> None:
        """Retry connecting a dropped account with exponential backoff.

        A FRESH client is built per attempt (factory + callback rewiring).
        Settings are re-read per attempt so live changes apply immediately.
        ``KeyboardInterrupt``/``SystemExit`` mark the account failed and stop
        reconnecting — they must never kill the process (ported from the
        audited ``_reconnect_loop``).
        """
        if gen is None:
            gen = rt.generation
        me = asyncio.current_task()
        attempt = 0
        while not rt.stop_requested and rt.generation == gen:
            if name not in self._accounts:
                return
            if rt.supervisor is not me:
                return  # stale supervisor: a newer owner exists
            cfg = self._reconnect_settings(name)
            if not cfg["enabled"]:
                # stand down WITHOUT leaving the re-entry conditions set:
                # status RECONNECTING / lost set would spin _supervise (see
                # the yield note there) — settle the account as failed
                rt.status = AccountStatus.FAILED
                rt.last_error = rt.last_error or "connection lost (reconnect disabled)"
                rt.lost.clear()
                self._logger.info(
                    "reconnect disabled for %s; standing down (status %s)",
                    name,
                    rt.status.value,
                )
                return
            # M2/S2-hardening: escalate SMOOTHLY per short-lived session
            # (the previous >=4-threshold cliff let an account hammer at
            # minimum backoff for three full session-cycles, then jumped
            # 16x). The floor IS the stable-window reset: a session that
            # survives >= 90s clears rt.flaps (see _on_disconnect), so
            # backoff resets only after a meaningful stable connection.
            now = time.monotonic()
            rt.flaps = [t for t in rt.flaps if now - t < 300.0]
            if attempt < len(rt.flaps):
                attempt = len(rt.flaps)
                if attempt >= 3:
                    self._logger.warning(
                        "account %s: %d short-lived sessions in the last"
                        " 5 min — Discord is closing this account's"
                        " sessions shortly after connect (young/flagged"
                        " account?); reconnect backoff escalated",
                        name,
                        len(rt.flaps),
                    )
            # Round-3 (backoff-overflow zombie): clamp the exponent and
            # guard the power itself. The clamp covers the default policy
            # (2.0 ** 1025 overflows ~21h into an outage); the OverflowError
            # guard covers pathological multipliers (1e300 ** 2) that would
            # otherwise kill the supervisor and leave the account stuck in
            # RECONNECTING forever with nobody driving it.
            try:
                delay = min(
                    cfg["initial"] * (cfg["multiplier"] ** min(attempt, _BACKOFF_EXPONENT_CAP)),
                    cfg["max"],
                )
            except OverflowError:
                delay = cfg["max"]
            # K9 (S7-A5): jitter the backoff per account — pure exponential
            # delays re-cluster synchronized reconnect storms under a shared
            # drop event, re-tripping Discord's per-IP IDENTIFY limit even
            # with the identify gate in place. NOTE: jitter is applied AFTER
            # the cap, so ``max`` is a soft ceiling (up to 1.5x) — accepted:
            # the anti-thundering-herd property outweighs the overshoot.
            delay *= self._typing_rng.uniform(1.0, 1.5)
            self._logger.warning(
                "account %s reconnect attempt %d in %.2fs", name, attempt + 1, delay
            )
            await self._sleep(delay)
            if rt.stop_requested or rt.generation != gen:
                return
            try:
                await self._connect_client(name, rt)
            except asyncio.CancelledError:
                raise
            except _StaleGeneration:
                return  # a newer lifecycle operation owns this account
            except (KeyboardInterrupt, SystemExit) as exc:
                if rt.stop_requested or rt.generation != gen:
                    return
                rt.status = AccountStatus.FAILED
                rt.lost.clear()  # stand down: the supervisor must not re-enter
                rt.last_error = f"reconnect interrupted: {exc}"
                self._logger.error("reconnect for %s interrupted: %r", name, exc)
                return
            except Exception as exc:
                if rt.stop_requested or rt.generation != gen:
                    # stop() won the race: its teardown already ran. Never
                    # resurrect status/publish past a completed stop.
                    return
                # F1.1: a FATAL gateway close (4004/4010-4014) or an
                # auth-shaped login failure is permanent — retrying a
                # revoked token forever burned a login() per backoff and
                # left the account in permanent RECONNECTING
                probe = rt.client
                if getattr(probe, "fatal_close", False) or _looks_like_auth_error(exc):
                    rt.status = AccountStatus.INVALID_CREDENTIAL
                    # stand down: with ``lost`` still set the supervisor
                    # re-entered this loop at once and retried the revoked
                    # token every ~1s until stop()
                    rt.lost.clear()
                    rt.last_error = f"{type(exc).__name__}: {exc}"
                    if rt.client is not None:
                        # _connect_client assigns rt.client BEFORE connect()
                        # raises — the failed client (and its run task) must
                        # not linger on an account nobody supervises
                        self._discard_half_built_client(name, rt)
                    self._lifecycle_commit("reconnect", name, ok=False, invalid_credential=True)
                    self._logger.error("reconnect for %s abandoned: credential rejected", name)
                    return
                attempt += 1
                if not rt.stop_requested:
                    # FIX (S1 #3): never regress STOPPED back to RECONNECTING
                    rt.status = AccountStatus.RECONNECTING
                rt.last_error = f"{type(exc).__name__}: {exc}"
                self._logger.warning("reconnect attempt failed for %s: %s", name, rt.last_error)
                continue
            if self._stop_landed(rt):
                # K3 (S1-C2): a stop() completed WHILE we were inside
                # connect() — its disconnect may have raced past this fresh
                # client. Do not resurrect the account: tear the client down
                # and leave it stopped (send_hook's RUNNING gate stays shut).
                client = rt.client
                rt.client = None
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        self._logger.exception("reconnect-abort disconnect failed for %s", name)
                return
            if rt.generation != gen:
                # Stale-start audit: the generation bump came from a NEWER
                # START (stop_requested was ruled out above). rt.client and
                # rt.status belong to the newer owner — leave them
                # untouched; our own fresh client was already released by
                # _connect_client's stale path before it returned.
                self._logger.info("reconnect of %s superseded by a newer start", name)
                return
            rt.status = AccountStatus.RUNNING
            rt.started_at = time.monotonic()  # uptime = current connection
            rt.session_started = rt.started_at  # M2: flap window starts at READY
            rt.last_error = None
            rt.lost.clear()
            self._logger.info("account %s reconnected (user_id=%s)", name, rt.user_id)
            self._bus.publish_nowait(AccountConnected(account=name))
            return

    async def _connect_client(self, name: str, rt: AccountRuntime) -> None:
        """Build a client, wire callbacks, connect and ingest the cache.

        Raises CredentialsError (no resolvable credential — DB store absent
        AND the legacy .env key absent when a credential service is wired;
        missing .env key otherwise), whatever the factory raises, or
        AccountError when connect() fails — ``start``/reconnect translate.
        PHASE-4: raises ``_StaleGeneration`` when a newer lifecycle
        operation took ownership mid-connect; callers treat it as a quiet
        abort, never as a failure.
        """
        gen = rt.generation
        account = self._accounts.get(name)
        if account is None:
            raise AccountError(f"unknown account {name!r}")
        token = await self._resolve_token(account.record)
        if rt.generation != gen:
            raise _StaleGeneration(name, gen)
        client = self._client_factory(name, token)
        old = rt.client
        if old is not None and old is not client:
            # reconnect path: the previous client may have auto-resumed its
            # gateway on its own — tear it down (best-effort) so its still-
            # wired callbacks can never double-publish events
            rt.client = None
            try:
                await old.disconnect()
            except Exception as exc:
                self._logger.warning("stale client teardown for %s raised %s", name, exc)
        if rt.generation != gen:
            # ownership changed during the old-client teardown: disconnect
            # the freshly built one too — never install it
            try:
                d = client.disconnect()
                if asyncio.iscoroutine(d):
                    await d
            except Exception:
                pass
            raise _StaleGeneration(name, gen)
        self._wire_client(name, rt, client)
        rt.client = client
        rt.status = AccountStatus.CONNECTING
        await client.connect()
        if rt.generation != gen:
            # a stop() completed while connect() was parked: tear the fresh
            # client down rather than letting the caller install state. If
            # rt still points at THIS client, hand it back first (the stop's
            # own teardown may not have reached it yet) so the stale
            # callers' discard paths see None and never double-disconnect.
            if rt.client is client:
                rt.client = None
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise _StaleGeneration(name, gen)
        if getattr(client, "connected", None) is False:  # protocol extra, tolerant
            raise AccountError(f"client for account {name!r} did not connect")
        rt.user_id = getattr(client, "user_id", None)
        rt.require_cache().ingest_snapshot(client.cached_snapshot())
        await self._record_access_user_id(name, rt.user_id)

    async def _record_access_user_id(self, name: str, user_id: Any) -> None:
        """Record the account's Discord id on its access row so the
        per-account admin flag can gate (best effort: never fails a connect)."""
        if not user_id:
            return
        if name not in getattr(self, "_accounts", {name: None}):
            return  # removed mid-connect: never resurrect its access row
        try:
            from ..plugin_manager.commands import COMMAND_HUB

            access = COMMAND_HUB.access
            if access is not None:
                await access.set_account_user_id(name, user_id)
        except Exception as exc:
            self._logger.debug("access user_id record failed for %s: %s", name, exc)

    def _wire_client(self, name: str, rt: AccountRuntime, client: Any) -> None:
        """Wire client callbacks BEFORE connect (pinned ordering).

        Every closure is guarded against staleness: a replaced client's
        callbacks (a reconnect built client #2 while #1 auto-resumed) must
        never publish events — ``rt.client is not client`` means this wiring
        has been superseded.
        """

        def _on_message(message: DiscordMessage) -> None:
            """Client callback -> fire-and-forget MessageReceived on the bus.

            The rich DTO rides along (``.message``): flat fields stay the
            compatibility surface; embeds/components/... are additive.
            """
            if rt.client is not client:
                return
            if rt.cache is not None and (message.dm or message.guild_id is not None):
                # cache learning is best-effort by contract; inbound traffic
                # TEACHES the cache: channels created after READY must not
                # fail validate_channel forever
                with contextlib.suppress(Exception):
                    rt.cache.learn_channel(
                        int(message.channel_id),
                        getattr(message, "guild_id", None),
                    )
            self._bus.publish_nowait(
                MessageReceived(
                    account=name,
                    message_id=message.id,
                    channel_id=message.channel_id,
                    author_id=message.author_id,
                    content=message.content,
                    dm=message.dm,
                    message=message,
                )
            )

        def _on_reaction(
            kind: str,
            message_id: int,
            channel_id: int,
            guild_id: int | None,
            user_id: int,
            emoji: Any,
            raw: Any,
        ) -> None:
            """Client callback -> ReactionAdded/Removed-shaped bus events.

            The stable ids + printable emoji cross WITHOUT any message fetch
            (§9); ``raw`` is the runtime-only library handle.
            """
            from ..event_bus import ReactionAdded

            if rt.client is not client:
                return
            if kind != "add":  # removal events: reserved for future plugins
                return
            text = emoji if isinstance(emoji, str) else getattr(emoji, "name", str(emoji))
            channel = rt.require_cache().channels.get(channel_id)
            self._bus.publish_nowait(
                ReactionAdded(
                    account=name,
                    message_id=int(message_id),
                    channel_id=int(channel_id),
                    guild_id=guild_id,
                    user_id=int(user_id),
                    emoji=text,
                    dm=channel.dm if channel is not None else False,
                    raw=raw,
                )
            )

        def _on_message_edit(message: DiscordMessage) -> None:
            from ..event_bus import MessageEdited

            if rt.client is not client:
                return
            self._bus.publish_nowait(
                MessageEdited(
                    account=name,
                    message_id=message.id,
                    channel_id=message.channel_id,
                    guild_id=message.guild_id,
                    message=message,
                )
            )

        def _on_message_delete(
            message_id: int, channel_id: int, guild_id: int | None, raw: Any
        ) -> None:
            from ..event_bus import MessageDeleted

            if rt.client is not client:
                return
            self._bus.publish_nowait(
                MessageDeleted(
                    account=name,
                    message_id=int(message_id),
                    channel_id=int(channel_id),
                    guild_id=guild_id,
                )
            )

        def _on_disconnect(reason: str = "") -> None:
            """Client callback on UNEXPECTED loss; deliberate stops ignore it."""
            if rt.stop_requested or rt.client is not client:
                return
            if rt.status in (AccountStatus.RUNNING, AccountStatus.CONNECTING):
                # M2: record session flaps (short-lived sessions). A session
                # that survived >= 90s means the account is healthy again —
                # clear the history so one bad network hour doesn't stick.
                now = time.monotonic()
                rt.flaps = [t for t in rt.flaps if now - t < 300.0]
                if rt.session_started and now - rt.session_started >= 90.0:
                    rt.flaps.clear()
                elif rt.session_started:
                    rt.flaps.append(now)
                if rt.supervisor is None or rt.supervisor.done():
                    # nobody will act on RECONNECTING (no supervisor was
                    # spawned, or it DIED of an unexpected exception —
                    # PHASE-4 S2 #6: a dead supervisor must not wedge the
                    # account in RECONNECTING forever): FAILED keeps
                    # start() usable for automation.
                    # Stale-start audit: ALSO set ``lost`` — a gateway drop
                    # inside start()'s attach/activate window (supervisor
                    # spawned moments later) must be recoverable: the
                    # supervisor reconnects only on status==RECONNECTING or
                    # lost.is_set(). No stale-wake hazard: start() clears
                    # ``lost`` before connecting and stop() clears it on
                    # completion, so this set is only observable by a
                    # supervisor of the SAME generation.
                    rt.status = AccountStatus.FAILED
                    rt.lost.set()
                else:
                    rt.status = AccountStatus.RECONNECTING
                    rt.lost.set()
                rt.last_error = reason or "connection lost"
                self._logger.warning("account %s connection lost (%s)", name, rt.last_error)

        client.on_message = _on_message
        client.on_disconnect = _on_disconnect
        # rich-event callbacks: wired on every client; adapters that never
        # fire them simply leave these as inert assignments (duck-typed)
        client.on_reaction = _on_reaction
        client.on_message_edit = _on_message_edit
        client.on_message_delete = _on_message_delete

    def _start_failed(self, name: str, rt: AccountRuntime, exc: BaseException) -> None:
        """Record a start failure: classified status + last_error + AccountFailed.

        Status classification (heuristic, documented):
          * CredentialsError            -> MISSING_CREDENTIAL (nothing to
            connect with — store a credential, do not retry-loop it),
          * an auth-shaped login error  -> INVALID_CREDENTIAL (the token was
            REJECTED). Detected by message text containing 'bad token' or
            '401' (case-insensitive): the live client raises plain
            AccountError("... (bad token?)") / wraps the library's
            LoginFailure, so text matching is the portable seam,
          * anything else               -> FAILED (network/library/unknown).
        """
        if rt.stop_requested:
            # K4 (S10-C4): the connect was aborted by an intentional
            # concurrent stop() — the resulting "connected without a user
            # (bad token?)" style error is NOT a credential failure, and
            # STOPPED must never regress to FAILED/INVALID_CREDENTIAL.
            return
        if rt.client is not None:
            # the gateway CONNECTED but a later step raised (cache ingest,
            # activation, ...) — a live websocket on a FAILED account is a
            # zombie session nobody owns until stop()/remove() happens
            # to be called
            self._discard_half_built_client(name, rt)
        if isinstance(exc, CredentialsError):
            rt.status = AccountStatus.MISSING_CREDENTIAL
        elif _looks_like_auth_error(exc):
            rt.status = AccountStatus.INVALID_CREDENTIAL
        else:
            rt.status = AccountStatus.FAILED
        rt.last_error = str(exc) or f"{type(exc).__name__}: {exc!r}"
        self._lifecycle_commit(
            "start",
            name,
            ok=False,
            invalid_credential=rt.status == AccountStatus.INVALID_CREDENTIAL,
        )
        self._logger.error("account %s failed to start: %s", name, rt.last_error)
        self._bus.publish_nowait(AccountFailed(account=name, error=rt.last_error))

    # --------------------------------------------------------------- sending
    async def _maybe_typing(self, name: str, rt: AccountRuntime, channel_id: int) -> None:
        """Probabilistic typing indicator before a guild send (anti-flag).

        Real clients fire typing while composing; an account that NEVER does
        is a detectable outlier. Off by default (typing_probability=0.0 —
        zero extra API calls unless opted in; ~0.85 looks human). A typing
        failure must NEVER fail the send.
        """
        try:
            probability = float(
                self._settings.account_settings(name).get("typing_probability", 0.0)
            )
        except Exception:
            return
        if probability <= 0 or not hasattr(rt.client, "send_typing"):
            return
        if self._typing_rng.random() >= probability:
            return
        # cosmetic best-effort only
        with contextlib.suppress(Exception):
            await rt.client.send_typing(channel_id)

    async def send_hook(self, task: Any) -> DiscordMessage | None:
        """Scheduler sender: one task -> one validated, audited Discord send.

        Raises:
            AccountError: the task targets an unknown account (no retry).
            ConnectionError: the account is not running (scheduler-retryable —
                it will resolve once the supervisor reconnects).
            core.errors.TargetGone: cached channel no longer exists (NO retry
                — the flow is dead; propagated with the reason attached).
            SchedulerError: unsupported task action.
        """
        name = getattr(task, "account", None)
        if not isinstance(name, str) or name not in self._accounts:
            raise AccountError(f"task targets unknown account {name!r}")
        account = self._accounts[name]
        rt = account.runtime
        if rt.status != AccountStatus.RUNNING or rt.client is None:
            raise ConnectionError(f"account {name!r} is not running (status: {rt.status.value})")
        action = getattr(task, "action", None)
        client = rt.client
        cache = rt.require_cache()
        message: DiscordMessage
        if action == "dm":
            # resolution order: memory cache -> persistent api_cache -> ONE
            # client.open_dm (recorded in both, never fetched again)
            channel_id = await resolve_dm_channel(
                cache, client, int(task.user_id), name, self._api_cache
            )
            # send through the RESOLVED channel: client.send_dm(user_id)
            # would re-fetch the user + re-open the DM (2 redundant REST
            # calls per send — a classic selfbot flag vector)
            try:
                message = await client.send_message(channel_id, task.content)
            except Exception as exc:
                if type(exc).__name__ == "TargetGone":
                    await invalidate_dm_channel(cache, self._api_cache, int(task.user_id), name)
                raise
        elif action == "message":
            cache.validate_channel(int(task.channel_id))  # TargetGone propagates
            channel_id = int(task.channel_id)
            await self._maybe_typing(name, rt, channel_id)
            try:
                message = await client.send_message(channel_id, task.content)
            except Exception as exc:
                if type(exc).__name__ == "TargetGone":
                    # stale guild-channel cache must HEAL (deleted channel /
                    # revoked access / left guild): without this the cache
                    # keeps passing validate_channel and every future send
                    # re-fails forever (only a restart healed it)
                    cache.invalidate_channel(channel_id, str(exc) or "send failed")
                raise
        elif action == "reaction":
            # kakerareact capability: emoji rides in task.content; target
            # channel validated like a send, message id is opaque to us
            cache.validate_channel(int(task.channel_id))
            channel_id = int(task.channel_id)
            try:
                await client.add_reaction(channel_id, int(task.message_id), task.content)
            except Exception as exc:
                if type(exc).__name__ == "TargetGone":
                    cache.invalidate_channel(channel_id, str(exc) or "reaction failed")
                raise
            return None  # no DiscordMessage shape for reactions
        elif action == "button":
            # kakeraloot capability: custom_id rides in task.content; the
            # adapter resolves the live message ON DEMAND (plugin-requested
            # fetch — the only fetch this action performs)
            cache.validate_channel(int(task.channel_id))
            channel_id = int(task.channel_id)
            try:
                await client.click_button(channel_id, int(task.message_id), task.content)
            except Exception as exc:
                if type(exc).__name__ == "TargetGone":
                    cache.invalidate_channel(channel_id, str(exc) or "button failed")
                raise
            return None
        else:
            raise SchedulerError(
                f"unsupported action {action!r} (supported: 'message', 'dm', 'reaction', 'button')"
            )
        # NOTE: outbound AUDIT is written by the scheduler at task-terminal
        # (exactly-once, with attempts context) — recording here too would
        # duplicate every send in the trail (found at integration).
        return message

    # -------------------------------------------------------------- details
    async def details(self, name: str) -> dict[str, Any]:
        """Account details page (spec §8) — EXACT shape:

        ``{"name", "status": {"connected","state","latency_ms","uptime_s"},
        "identity": {"name","discord_id"}, "runtime": {"cached_guilds",
        "cached_channels","cached_dms","cache_source"}, "plugins":
        [{"name","enabled"}], "profiles": [...], "rate_limit": {...},
        "credential": "db"|"env"|None}``

        Everything runtime-y draws ONLY from the live client's ingested cache.
        ``rate_limit`` is built only when a limiter was injected: actions come
        from ``limiter.policies()``, effect from ``limiter.effective(action,
        name)``; ``source`` is ``"inherited from global"`` when at most one
        level applies, else the comma-joined ``level:owner`` list (owner
        omitted for the global level, which has none).

        Async: the persistent record (profiles, enabled flag) is re-read
        from the repository here.
        """
        account = self._get(name)
        rt = account.runtime
        await self._refresh_record(account)
        self._refresh_credential_source(account)
        record = account.record
        client = rt.client
        state = rt.status.value
        cache = rt.require_cache()
        latency = getattr(client, "latency_ms", None) if client is not None else None
        uptime = None
        if rt.started_at is not None and rt.status == AccountStatus.RUNNING:
            uptime = round(max(0.0, time.monotonic() - rt.started_at), 1)
        return {
            "name": name,
            "status": {
                "connected": rt.status == AccountStatus.RUNNING,
                "state": state,
                "latency_ms": latency,
                "uptime_s": uptime,
            },
            "identity": {
                "name": cache.user_name or record.name,
                "discord_id": cache.user_id if cache.user_id is not None else rt.user_id,
            },
            "runtime": {
                "cached_guilds": len(cache.guilds),
                "cached_channels": len(cache.channels),
                "cached_dms": cache.dms(),
                "cache_source": "client cache (READY)",
            },
            "plugins": self._plugin_rows(account),
            "rate_limit": self._rate_limit_rows(name),
            "credential": account.credential_source,
        }

    def _rate_limit_rows(self, name: str) -> dict[str, dict[str, Any]]:
        """Effective rate policy per action (empty dict without a limiter)."""
        if self._limiter is None:
            return {}
        try:
            policies = self._limiter.policies() or []
            rows: dict[str, dict[str, Any]] = {}
            for policy in policies:
                action = policy.get("action")
                if not action or action in rows:
                    continue
                effective = self._limiter.effective(action, name) or {}
                sources = effective.get("sources") or []
                if len(sources) <= 1:
                    source = "inherited from global"
                else:
                    source = ", ".join(
                        f"{s.get('level')}:{s.get('owner')}"
                        if s.get("owner")
                        else str(s.get("level"))
                        for s in sources
                    )
                rows[action] = {
                    "interval": effective.get("interval"),
                    "jitter": effective.get("jitter"),
                    "source": source,
                }
            return rows
        except Exception:
            self._logger.exception("could not build rate_limit details for %s", name)
            return {}

    def _plugin_rows(self, account: Account) -> list[dict[str, Any]]:
        """[{"name","enabled"}] — from plugin_manager.states when it has the
        account, else the Account's last known plugin view."""
        name = account.record.name
        try:
            states = self._plugin_manager.states(name)
        except Exception:
            states = None
        if states:
            return [
                {"name": s.get("name"), "enabled": bool(s.get("enabled", True))}
                for s in states
                if isinstance(s, dict) and s.get("name")
            ]
        return [
            {"name": pname, "enabled": bool(info.get("enabled", True))}
            for pname, info in sorted(account.plugins.items())
        ]

    # ----------------------------------------------------------- services
    def _services_factory(self, name: str) -> Callable[[str], AccountPluginServices]:
        """Build the per-plugin services factory handed to activate_account."""

        def factory(plugin_name: str) -> AccountPluginServices:
            return self.services_for(name, plugin_name)

        return factory

    def services_for(self, account_name: str, plugin_name: str) -> AccountPluginServices:
        """One AccountPluginServices per (account, plugin); cached per start.

        The settings dict is seeded from ``plugin_manager.states`` when
        available — the plugin manager refreshes that same dict in place, so
        ``services.settings`` stays a live view. When the manager was built
        with a CooldownService / PluginStateService, both are handed to the
        facade (pair-scoped ``cooldown_*`` / ``state_*`` plugin API).
        """
        key = (account_name, plugin_name)
        if key in self._services:
            return self._services[key]
        account = self._accounts.get(account_name)
        cache = (
            account.runtime.require_cache() if account is not None else DiscordCache(account_name)
        )
        settings_dict: dict[str, Any] = {}
        try:
            # LIVE view first (set_setting mutates this dict in place, so
            # services.settings and ctx.settings can never diverge); the
            # states() copy is only a fallback for duck-typed collaborators
            live: Any = getattr(self._plugin_manager, "live_settings", None)
            live_view = live(account_name, plugin_name) if callable(live) else None
            if isinstance(live_view, dict):
                settings_dict = live_view
            else:
                settings_dict = {}
                for state in self._plugin_manager.states(account_name) or []:
                    if isinstance(state, dict) and state.get("name") == plugin_name:
                        settings_dict = state.get("settings") or {}
                        break
        except Exception:
            pass
        services = AccountPluginServices(
            account_name,
            plugin_name,
            bus=self._bus,
            scheduler=self._scheduler,
            credentials=self._credentials,
            cache=cache,
            settings_dict=settings_dict,
            ack_registry=self._ack_registry,
            waiter_registry=self._waiter_registry,
            client_getter=lambda: self._client_of(account_name),
            api_cache_repo=self._api_cache,
            cooldowns=self._cooldowns,
            plugin_state=self._plugin_state,
            plugin_data=self._plugin_data,
        )
        self._services[key] = services
        return services

    def _client_of(self, name: str) -> Any:
        """Live client of an account (None when absent/stopped)."""
        account = self._accounts.get(name)
        return account.runtime.client if account is not None else None

    def _drop_services(self, name: str) -> None:
        """Forget the account's per-plugin services (rebuilt on next start)."""
        for key in [k for k in self._services if k[0] == name]:
            del self._services[key]

    # --------------------------------------------------------------- plugins
    async def _attach_plugins(self, name: str) -> bool:
        """attach_account with the account's CURRENT plugin assignments.

        Returns True when the plugins were (re-)activated here (a
        RUNNING account follows assignments immediately) — callers that
        would otherwise activate right after (``start``) must skip
        their own activation then, or hooks fire twice.

        The allowed set is ALWAYS concrete now (a direct read of the
        in-memory assignment mirror — zero assignments attach zero
        plugins); the old ``None = unrestricted`` profile default is gone
        from this path. The plugin manager's attach is async (it reads
        persisted enabled state from the repository); a collaborator
        returning plain None (sync duck-typed doubles) is accepted
        unchanged. A SUCCESSFUL attach marks the account as
        plugin-manager-attached (manager-side view used by
        ``_refresh_plugins``); a failed attach leaves it unmarked so the
        last known plugin view is kept rather than cleared.
        """
        activated = False
        try:
            outcome = self._plugin_manager.attach_account(name, self._allowed_plugins(name))
            if inspect.iscoroutine(outcome):
                await outcome
        except Exception:
            self._logger.exception("plugin attach failed for %s", name)
            return False
        self._plugin_attached.add(name)
        # A RUNNING account's plugin set must follow the assignment
        # IMMEDIATELY (documented contract) — attach_account rebuilds
        # runtimes as inactive and deactivates the old set, so without
        # re-activation every mid-run assign/unassign/set silently
        # stopped ALL of the account's plugins until the next start.
        rt = self._accounts.get(name)
        if (
            rt is not None
            and rt.runtime.status == AccountStatus.RUNNING
            and not rt.runtime.stop_requested
        ):
            try:
                self._drop_services(name)
                outcome = self._plugin_manager.activate_account(name, self._services_factory(name))
                if inspect.iscoroutine(outcome):
                    await outcome
                activated = True
            except Exception:
                self._logger.exception(
                    "plugin re-activation after attach failed for %s "
                    "(plugins stay inactive until the next start)",
                    name,
                )
            if self._stop_landed(rt.runtime):
                # a stop() completed DURING the activation await (review
                # finding #3): its teardown ran before these hooks fired —
                # deactivate again so no active plugin outlives the stop
                self._logger.warning(
                    "stop landed during attach-activation of %s; deactivating its plugins", name
                )
                try:
                    outcome = self._plugin_manager.deactivate_account(name)
                    if inspect.iscoroutine(outcome):
                        await outcome
                    activated = False
                except Exception:
                    self._logger.exception(
                        "post-stop deactivation failed for %s (plugins "
                        "may linger active until the next stop)",
                        name,
                    )
        return activated

    def _allowed_plugins(self, name: str) -> set[str]:
        """The account's DIRECT plugin assignments — NEVER None.

        Source of truth: ``Account.assigned_plugins`` (the in-memory mirror
        of the ``account_plugins`` table, bulk-loaded at boot and rewritten
        at every assignment mutation). ProfileManager is NOT consulted:
        profiles no longer mediate capabilities, and "no assignment =
        unrestricted" is gone — an unknown account or an empty assignment
        set attaches ZERO plugins by construction (fail-closed).
        """
        account = self._accounts.get(name)
        if account is None:
            return set()
        return {p for p in (account.assigned_plugins or []) if isinstance(p, str)}

    def refresh_workspace(self, name: str, workspace: str | None) -> None:
        """Mirror a workspace-membership change into the in-memory
        record (FINAL-PHASE F3 pattern): status snapshots read
        record.workspace. The WorkspaceService owns the persisted
        pointer (and the pacing registration); this keeps the account
        manager's view honest without a second writer."""
        account = self._accounts.get(name)
        if account is not None:
            account.record.workspace = workspace or None

    def _refresh_plugins(self, account: Account) -> None:
        """Refresh Account.plugins from plugin_manager.states (keep last known).

        An EMPTY states() answer is ambiguous on its own (both a missing
        account and an attached-with-zero-plugins account return ``[]``), so
        the manager-side ``_plugin_attached`` view breaks the tie: attached +
        empty means a fail-closed re-attach genuinely left the account with
        no plugins — the stale view is cleared (round-3: it previously kept
        showing plugins that were no longer attached). Not attached (yet)
        keeps the last known view, as before.
        """
        name = account.record.name
        try:
            states = self._plugin_manager.states(name)
        except Exception:
            return
        if not states:
            if name in self._plugin_attached:
                account.plugins = {}  # attached with zero plugins: the truth
            return  # plugin manager does not have the account (yet) — keep old
        plugins: dict[str, dict[str, Any]] = {}
        for state in states:
            if not isinstance(state, dict) or not state.get("name"):
                continue
            plugins[state["name"]] = {
                "enabled": bool(state.get("enabled", True)),
                "last_error": state.get("last_error"),
                "error_count": int(state.get("error_count", 0) or 0),
            }
        if plugins:
            account.plugins = plugins

    # ----------------------------------------------------------------- audit
    @staticmethod
    def _rich_note(event: MessageReceived) -> str:
        """Embed/component counts for the inbound audit note (§21: metadata,
        never payloads)."""
        message = getattr(event, "message", None)
        if message is None:
            return ""
        parts = []
        if message.embeds:
            parts.append(f"embeds={len(message.embeds)}")
        if message.components:
            parts.append(f"components={len(message.components)}")
        if message.attachments:
            parts.append(f"attachments={len(message.attachments)}")
        return (" " + " ".join(parts)) if parts else ""

    def _audit_inbound(self, event: MessageReceived) -> None:
        """MessageReceived -> inbound audit row (never raises; guarded)."""
        if self._audit is None:
            return
        try:
            preview = (event.content or "").replace("\n", "\\n")[:80]
            self._audit.record(
                account=event.account,
                direction="in",
                action="message",
                channel_id=event.channel_id,
                content=preview,
                note=f"author={event.author_id}{' dm' if event.dm else ''}"
                + self._rich_note(event),
            )
        except Exception:
            self._logger.exception("inbound audit recording failed")

    def _audit_out(
        self,
        *,
        account: str,
        action: str,
        channel_id: int,
        content: Any,
        wait_ms: float,
        jitter_ms: float,
        note: str = "",
    ) -> None:
        """Outbound audit row for one send (never raises; guarded)."""
        if self._audit is None:
            return
        try:
            preview = (content or "").replace("\n", "\\n")[:80]
            self._audit.record(
                account=account,
                direction="out",
                action=action,
                channel_id=channel_id,
                content=preview,
                wait_ms=float(wait_ms),
                jitter_ms=float(jitter_ms),
                note=note,
            )
        except Exception:
            self._logger.exception("outbound audit recording failed")

    # --------------------------------------------------------------- helpers
    def _get(self, name: str) -> Account:
        """Account by name with human errors (unknown / unhashable)."""
        try:
            account = self._accounts.get(name)
        except TypeError as exc:  # unhashable payloads via the dashboard
            raise AccountError(f"account name must be a string, got {name!r}") from exc
        if account is None:
            raise AccountError(f"unknown account {name!r} (registered: {sorted(self._accounts)})")
        return account

    async def _repo_get(self, name: str) -> AccountRecord | None:
        """Fresh record from the repository (None on failure/unknown)."""
        try:
            # boundary annotation: the repo seam is Any (duck-typed doubles
            # in tests); AccountRecord is the contract every real repo returns
            record: AccountRecord | None = await self._accounts_repo.get(name)
            return record
        except Exception:
            self._logger.exception("could not read account %r from repository", name)
            return None

    async def _refresh_record(self, account: Account) -> None:
        """Pull persistent state (profiles, enabled...) from the repository."""
        fresh = await self._repo_get(account.record.name)
        if fresh is not None:
            account.record = fresh

    def _refresh_credential_source(self, account: Account) -> None:
        """Mirror ``CredentialService.status(name)`` onto the Account (display
        snapshot field; None when no credential service is wired — never
        token material)."""
        if self._credential_service is None:
            account.credential_source = None
            return
        try:
            account.credential_source = self._credential_service.status(account.record.name) or None
        except Exception:
            account.credential_source = None

    async def _resolve_token(self, record: AccountRecord) -> str:
        """Token value for an account, DB-first when a credential service is
        wired (stored credential -> legacy .env convention key -> error);
        legacy .env lookup only otherwise (back-compat for unwired tests).

        Raises:
            CredentialsError: actionable message when nothing resolves.
        """
        if self._credential_service is not None:
            # boundary annotation: the credential seam is Any (duck-typed
            # doubles in tests); a resolved token is the str contract
            token: str = await self._credential_service.resolve(record.name)
            if not token:
                raise CredentialsError(
                    f"account {record.name!r} has no stored credential — "
                    "add one through the dashboard (Accounts → credential…) "
                    "or the CLI 'accounts credential <name> <token>'"
                )
            return token
        # no credential service wired (tests): nothing can resolve
        raise CredentialsError(
            f"account {record.name!r} cannot start: credential storage "
            "is not configured on this framework instance"
        )

    def _reconnect_settings(self, name: str) -> dict[str, float]:
        """Account reconnect policy via SettingsManager (duck-typed, defaults
        on any failure so a settings problem never blocks reconnection)."""
        try:
            raw = self._settings.account_settings(name) or {}
        except Exception:
            raw = {}
        try:
            return {
                "enabled": bool(raw.get("reconnect_enabled", True)),
                "initial": max(
                    0.0, float(raw.get("reconnect_initial_delay", _DEFAULT_RECONNECT["initial"]))
                ),
                "max": max(0.1, float(raw.get("reconnect_max_delay", _DEFAULT_RECONNECT["max"]))),
                "multiplier": max(
                    1.0, float(raw.get("reconnect_multiplier", _DEFAULT_RECONNECT["multiplier"]))
                ),
            }
        except (TypeError, ValueError):
            return {"enabled": True, **_DEFAULT_RECONNECT}


__all__ = ["NAME_RE", "Account", "AccountManager", "AccountRuntime", "AccountStatus"]
