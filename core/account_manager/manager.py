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
(``_sync_group_scope``) is pure plumbing — no API involved.
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
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from ..credentials import Credentials
from ..errors import AccountError, CredentialsError, ProfileError, SchedulerError
from ..event_bus import (
    AccountConnected,
    AccountDisconnected,
    AccountFailed,
    EventBus,
    MessageReceived,
)
from ..settings_manager import SettingsManager
from adapters.base import DiscordMessage
from database.models import AccountRecord
from .cache import DiscordCache, invalidate_dm_channel, resolve_dm_channel
from .services import AccountPluginServices, TaskAckRegistry

#: account names share the profile namespace style (may start with a digit)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: fallback reconnect policy when the settings manager is unavailable/blank
_DEFAULT_RECONNECT = {"initial": 1.0, "max": 60.0, "multiplier": 2.0}


class AccountStatus(str, Enum):
    """Lifecycle states of one account's connection."""

    CREATED = "created"
    CONNECTING = "connecting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class AccountRuntime:
    """RUNTIME state of one account (never persisted).

    ``lost`` extends the pinned field set: an asyncio.Event set by the
    disconnect callback on UNEXPECTED drops; the supervisor waits on it.
    ``started_at`` uses ``time.monotonic()`` (uptime is wall-clock-jump safe).
    """

    status: AccountStatus = AccountStatus.CREATED
    client: Any = None
    cache: Optional[DiscordCache] = None
    started_at: Optional[float] = None
    last_error: Optional[str] = None
    user_id: Optional[int] = None
    supervisor: Optional["asyncio.Task[None]"] = None
    stop_requested: bool = False
    lost: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Account:
    """First-class account object: persistent record + runtime + plugin view.

    ``plugins`` caches the last known per-plugin runtime info
    (``{name: {"enabled","last_error","error_count"}}``); it is refreshed from
    ``plugin_manager.states()`` whenever a status snapshot is built and the
    plugin manager has the account, and keeps the last known values otherwise.
    """

    record: AccountRecord
    runtime: AccountRuntime = field(default_factory=AccountRuntime)
    plugins: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Every account owns exactly one DiscordCache for its whole life."""
        if self.runtime.cache is None:
            self.runtime.cache = DiscordCache(self.record.name)

    # -------------------------------------------------------------- status
    def status_dict(self) -> Dict[str, Any]:
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
            "profiles": list(self.record.profiles),
            "latency_ms": latency,
            "uptime_s": uptime,
            "last_error": rt.last_error,
            "plugins": {
                name: dict(info) for name, info in sorted(self.plugins.items())
            },
        }


class AccountManager:
    """Owns every account: registry, lifecycle, send hook, details page."""

    def __init__(
        self,
        bus: EventBus,
        logger: Any,
        accounts_repo: Any,
        plugin_manager: Any,
        profile_manager: Any,
        scheduler: Any,
        settings: SettingsManager,
        audit: Any,
        client_factory: Callable[[str, str], Any],
        credentials: Credentials,
        *,
        limiter: Any = None,
        supervisor_tick: float = 0.2,
        api_cache_repo: Any = None,
    ) -> None:
        """``limiter`` (optional) enriches details() with effective rate data;
        ``supervisor_tick`` is the supervisor's poll granularity in seconds;
        ``api_cache_repo`` (optional) persists resolved Discord entities
        (DM channels, users) per account so restarts never re-fetch them.
        """
        self._bus = bus
        self._logger = logger or logging.getLogger("slaughtermudae.accounts")
        self._accounts_repo = accounts_repo
        self._plugin_manager = plugin_manager
        self._profile_manager = profile_manager
        self._scheduler = scheduler
        self._settings = settings
        self._audit = audit
        self._client_factory = client_factory
        self._credentials = credentials
        self._limiter = limiter
        self._supervisor_tick = float(supervisor_tick)
        self._api_cache = api_cache_repo
        # typing-indicator RNG (anti-flag: humans trigger typing; disabled
        # by default via the typing_probability account setting = 0.0)
        import random as _random
        self._typing_rng = _random.Random()

        self._accounts: Dict[str, Account] = {}
        #: ONE task-ack registry per manager (single bus subscription set)
        self._ack_registry = TaskAckRegistry(bus)
        #: (account, plugin) -> services; rebuilt on each activation
        self._services: Dict[tuple, AccountPluginServices] = {}
        #: strong refs for fire-and-forget tasks (documented create_task hazard)
        self._background: set = set()

        # inbound audit trail (spec §10): every message seen by any account
        bus.subscribe(MessageReceived, self._audit_inbound)

        # restore accounts persisted in the database (boot path)
        self._load_from_repo()

    # ------------------------------------------------------- rate-scope sync
    def _sync_group_scope(self, name: str, group: Optional[str]) -> None:
        """Feed the account's group membership to the limiter (per-account/
        group rate scope). Pure metadata plumbing — NO Discord API is
        involved; tolerant of limiters without group support (tests)."""
        register = getattr(self._limiter, "set_account_group", None)
        if register is None:
            return
        try:
            register(name, group)
        except Exception:
            self._logger.exception("group scope registration failed for %s", name)

    # ------------------------------------------------------------------ boot
    def _load_from_repo(self) -> None:
        """Rebuild Account objects (and attach their plugins) from the repo.

        Best-effort: a repository failure must not break construction.
        """
        try:
            records = self._accounts_repo.list() or []
        except Exception:
            self._logger.exception("could not load accounts from repository")
            return
        for record in records:
            try:
                account = Account(record=record)
                self._accounts[record.name] = account
                self._attach_plugins(record.name)
                self._sync_group_scope(record.name, record.sync_group)
            except Exception:
                self._logger.exception("could not restore account %r", record.name)

    # ------------------------------------------------------------ registry
    def add_account(
        self,
        name: str,
        *,
        token_ref: Optional[str] = None,
        enabled: bool = True,
        sync_group: Optional[str] = None,
        profiles: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Register + persist an account; returns its status dict.

        Persists to the repository FIRST (a failed write leaves memory
        untouched), then builds the in-memory Account and assigns profiles.
        The token VALUE never enters the database — only the .env key ref.

        Raises:
            AccountError: invalid name (must match NAME_RE) or duplicate.
            ProfileError: any requested profile is unknown.
        """
        if not isinstance(name, str) or not NAME_RE.match(name or ""):
            raise AccountError(
                f"account name {name!r} must match {NAME_RE.pattern} "
                "(lowercase letters/digits/_/-, at most 32 chars)"
            )
        if name in self._accounts:
            raise AccountError(f"account {name!r} already exists")
        if profiles is not None:
            if not isinstance(profiles, (list, tuple)) or not all(
                isinstance(p, str) for p in profiles
            ):
                raise AccountError("'profiles' must be a list of profile names")
            self._validate_profiles_known(profiles)
        record = self._accounts_repo.add(
            name, token_ref=token_ref, enabled=bool(enabled), sync_group=sync_group
        )
        account = Account(record=record)
        self._accounts[name] = account
        for profile in dict.fromkeys(profiles or []):
            try:
                self._profile_manager.assign(name, profile)
            except Exception:
                self._logger.exception(
                    "could not assign profile %r to new account %r", profile, name
                )
        fresh = self._repo_get(name)
        if fresh is not None:
            account.record = fresh
        self._attach_plugins(name)
        self._sync_group_scope(name, sync_group)
        self._logger.info(
            "account %s added (enabled=%s sync_group=%s profiles=%s)",
            name, enabled, sync_group, account.record.profiles or "-",
        )
        return account.status_dict()

    def remove_account(self, name: str) -> Dict[str, Any]:
        """Remove a STOPPED account: detach plugins, cancel supervisor, delete.

        Raises:
            AccountError: unknown account, or still running/reconnecting
                (stop it first — the refusal happens BEFORE any mutation).
        """
        account = self._get(name)
        rt = account.runtime
        if rt.status in (
            AccountStatus.RUNNING, AccountStatus.RECONNECTING, AccountStatus.CONNECTING,
        ):
            raise AccountError(
                f"stop account {name!r} before removing it "
                f"(status: {rt.status.value})"
            )
        rt.stop_requested = True
        supervisor = rt.supervisor
        rt.supervisor = None
        if supervisor is not None:  # sync context: cancel, the loop reaps it
            supervisor.cancel()
        try:
            self._plugin_manager.detach_account(name)
        except Exception:
            self._logger.exception("plugin detach failed for %s", name)
        self._drop_services(name)
        self._sync_group_scope(name, None)
        existed = bool(self._accounts_repo.delete(name))
        del self._accounts[name]
        self._logger.info("account %s removed (existed=%s)", name, existed)
        return {"removed": name, "existed": existed}

    def get(self, name: str) -> Account:
        """The Account object (AccountError when unknown / unhashable name)."""
        return self._get(name)

    def names(self) -> List[str]:
        """Sorted account names."""
        return sorted(self._accounts)

    async def accounts(self) -> List[Dict[str, Any]]:
        """Status dicts of every account, sorted by name (records refreshed)."""
        out: List[Dict[str, Any]] = []
        for name in sorted(self._accounts):
            account = self._accounts[name]
            self._refresh_record(account)
            self._refresh_plugins(account)
            out.append(account.status_dict())
        return out

    def set_enabled(self, name: str, enabled: bool) -> Dict[str, Any]:
        """Flip the persisted enabled flag (False = skipped by start_all)."""
        account = self._get(name)
        self._accounts_repo.set_enabled(name, bool(enabled))  # persist first
        account.record.enabled = bool(enabled)
        return account.status_dict()

    # ------------------------------------------------------------- lifecycle
    async def start(self, name: str) -> Dict[str, Any]:
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
            AccountStatus.RUNNING, AccountStatus.CONNECTING, AccountStatus.RECONNECTING,
        ):
            raise AccountError(f"account {name!r} is already {rt.status.value}")
        rt.stop_requested = False
        rt.lost.clear()
        rt.last_error = None
        rt.status = AccountStatus.CONNECTING
        try:
            await self._connect_client(name, rt)
        except asyncio.CancelledError:
            rt.status = AccountStatus.FAILED
            rt.last_error = "start cancelled"
            raise
        except (KeyboardInterrupt, SystemExit):
            raise  # process-level signals are never swallowed here
        except Exception as exc:
            self._start_failed(name, rt, exc)
            if isinstance(exc, (AccountError, CredentialsError)):
                raise
            raise AccountError(
                f"account {name!r} failed to connect: {exc}"
            ) from exc
        if rt.stop_requested:  # a stop() raced us mid-connect — honour it
            rt.status = AccountStatus.STOPPED
            self._logger.info("account %s start aborted by concurrent stop", name)
            return account.status_dict()
        rt.status = AccountStatus.RUNNING
        rt.started_at = time.monotonic()
        self._logger.info("account %s running (user_id=%s)", name, rt.user_id)
        # plugins: re-attach with the CURRENT profile grants (assignments
        # take effect on the next start — ported semantics), then activate
        self._attach_plugins(name)
        try:
            outcome = self._plugin_manager.activate_account(
                name, self._services_factory(name)
            )
            if asyncio.iscoroutine(outcome):  # pinned sync; async tolerated
                await outcome
        except Exception:
            self._logger.exception(
                "plugin activation failed for %s (account keeps running)", name
            )
        self._bus.publish_nowait(AccountConnected(account=name))
        if self._reconnect_settings(name)["enabled"]:
            rt.supervisor = asyncio.create_task(
                self._supervise(name, rt), name=f"supervise-{name}"
            )
        return account.status_dict()

    async def stop(self, name: str) -> Dict[str, Any]:
        """Stop one account (idempotent; plugins deactivated first).

        Order (pinned): stop_requested=True, deactivate plugins (hooks run
        while the cache is still available), ``await client.disconnect()``
        (None-safe), reap the supervisor (bounded 1s wait, then cancel),
        status stopped, publish ``AccountDisconnected(reason="stopped")``.
        """
        account = self._get(name)
        rt = account.runtime
        if rt.status == AccountStatus.STOPPED:
            return account.status_dict()  # idempotent: nothing re-fires
        was_live = rt.status in (
            AccountStatus.RUNNING, AccountStatus.RECONNECTING, AccountStatus.CONNECTING,
        ) or rt.client is not None
        rt.stop_requested = True
        rt.lost.set()  # wake the supervisor so it sees stop_requested NOW
        try:
            outcome = self._plugin_manager.deactivate_account(name)
            if asyncio.iscoroutine(outcome):  # pinned sync; async tolerated
                await outcome
        except Exception:
            self._logger.exception("plugin deactivation failed for %s", name)
        client = rt.client
        if client is not None:
            try:
                await client.disconnect()
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
            except asyncio.CancelledError:
                supervisor.cancel()  # caller cancelled us — don't leak the task
                raise
        rt.status = AccountStatus.STOPPED
        rt.lost.clear()
        self._drop_services(name)
        if was_live:
            self._bus.publish_nowait(AccountDisconnected(account=name, reason="stopped"))
        self._logger.info("account %s stopped", name)
        return account.status_dict()

    async def start_all(self) -> Dict[str, Any]:
        """Start every ENABLED account; logs and continues per account."""
        started: List[str] = []
        failed: Dict[str, str] = {}
        skipped: List[str] = []
        for name in sorted(self._accounts):
            account = self._accounts[name]
            if not account.record.enabled:
                skipped.append(name)
                self._logger.info("skipping disabled account %s", name)
                continue
            try:
                await self.start(name)
                started.append(name)
            except Exception as exc:
                failed[name] = f"{type(exc).__name__}: {exc}"
                self._logger.exception("failed to start account %s", name)
        return {"started": started, "failed": failed, "skipped": skipped}

    async def stop_all(self) -> Dict[str, Any]:
        """Graceful shutdown of every account; logs and continues per account."""
        stopped: List[str] = []
        failed: Dict[str, str] = {}
        for name in sorted(self._accounts):
            try:
                await self.stop(name)
                stopped.append(name)
            except Exception as exc:
                failed[name] = f"{type(exc).__name__}: {exc}"
                self._logger.exception("failed to stop account %s", name)
        return {"stopped": stopped, "failed": failed}

    def emergency_stop(self, name: str) -> Dict[str, Any]:
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
        rt.lost.set()             # wake the supervisor NOW
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
                "emergency_stop(...) needs a running event loop "
                "(call it from async context)"
            ) from exc
        task = loop.create_task(self.stop(name), name=f"estop-{name}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        self._logger.error(
            "EMERGENCY STOP %s: cancelled %d tasks, disconnecting", name, cancelled
        )
        return {"account": name, "cancelled_tasks": cancelled, "stop_initiated": True}

    # ------------------------------------------------------------ supervisor
    async def _supervise(self, name: str, rt: AccountRuntime) -> None:
        """Watch one account; reconnect with backoff after unexpected drops.

        Polls the ``lost`` event / status every ``supervisor_tick`` seconds
        (ported cadence). Deliberate stops set ``stop_requested`` and the
        supervisor stands down without reconnecting. Cancellation (stop with
        a stuck backoff sleep) propagates — the reaper in stop() handles it.
        """
        try:
            while not rt.stop_requested:
                if name not in self._accounts:  # removed mid-flight
                    return
                if rt.status == AccountStatus.RECONNECTING or rt.lost.is_set():
                    await self._reconnect_loop(name, rt)
                    continue
                try:
                    await asyncio.wait_for(
                        rt.lost.wait(), timeout=self._supervisor_tick
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise  # deliberate stop/removal — stand down, never swallow

    async def _reconnect_loop(self, name: str, rt: AccountRuntime) -> None:
        """Retry connecting a dropped account with exponential backoff.

        A FRESH client is built per attempt (factory + callback rewiring).
        Settings are re-read per attempt so live changes apply immediately.
        ``KeyboardInterrupt``/``SystemExit`` mark the account failed and stop
        reconnecting — they must never kill the process (ported from the
        audited ``_reconnect_loop``).
        """
        attempt = 0
        while not rt.stop_requested:
            if name not in self._accounts:
                return
            cfg = self._reconnect_settings(name)
            if not cfg["enabled"]:
                self._logger.info(
                    "reconnect disabled for %s; standing down (status stays %s)",
                    name, rt.status.value,
                )
                return
            delay = min(
                cfg["initial"] * (cfg["multiplier"] ** attempt), cfg["max"]
            )
            self._logger.warning(
                "account %s reconnect attempt %d in %.2fs", name, attempt + 1, delay
            )
            await asyncio.sleep(delay)
            if rt.stop_requested:
                return
            try:
                await self._connect_client(name, rt)
            except asyncio.CancelledError:
                raise
            except (KeyboardInterrupt, SystemExit) as exc:
                rt.status = AccountStatus.FAILED
                rt.last_error = f"reconnect interrupted: {exc}"
                self._logger.error("reconnect for %s interrupted: %r", name, exc)
                return
            except Exception as exc:
                attempt += 1
                rt.status = AccountStatus.RECONNECTING
                rt.last_error = f"{type(exc).__name__}: {exc}"
                self._logger.warning(
                    "reconnect attempt failed for %s: %s", name, rt.last_error
                )
                continue
            rt.status = AccountStatus.RUNNING
            rt.started_at = time.monotonic()  # uptime = current connection
            rt.last_error = None
            rt.lost.clear()
            self._logger.info("account %s reconnected (user_id=%s)", name, rt.user_id)
            self._bus.publish_nowait(AccountConnected(account=name))
            return

    async def _connect_client(self, name: str, rt: AccountRuntime) -> None:
        """Build a client, wire callbacks, connect and ingest the cache.

        Raises CredentialsError (missing token), whatever the factory raises,
        or AccountError when connect() fails — ``start``/reconnect translate.
        """
        account = self._accounts.get(name)
        if account is None:
            raise AccountError(f"unknown account {name!r}")
        token = self._resolve_token(account.record)
        client = self._client_factory(name, token)
        self._wire_client(name, rt, client)
        rt.client = client
        rt.status = AccountStatus.CONNECTING
        await client.connect()
        if getattr(client, "connected", None) is False:  # protocol extra, tolerant
            raise AccountError(f"client for account {name!r} did not connect")
        rt.user_id = getattr(client, "user_id", None)
        rt.cache.ingest_snapshot(client.cached_snapshot())

    def _wire_client(self, name: str, rt: AccountRuntime, client: Any) -> None:
        """Wire client callbacks BEFORE connect (pinned ordering)."""

        def _on_message(message: DiscordMessage) -> None:
            """Client callback -> fire-and-forget MessageReceived on the bus."""
            self._bus.publish_nowait(MessageReceived(
                account=name,
                message_id=message.id,
                channel_id=message.channel_id,
                author_id=message.author_id,
                content=message.content,
                dm=message.dm,
            ))

        def _on_disconnect(reason: str = "") -> None:
            """Client callback on UNEXPECTED loss; deliberate stops ignore it."""
            if rt.stop_requested:
                return
            if rt.status in (AccountStatus.RUNNING, AccountStatus.CONNECTING):
                rt.status = AccountStatus.RECONNECTING
                rt.last_error = reason or "connection lost"
                rt.lost.set()
                self._logger.warning(
                    "account %s connection lost (%s)", name, rt.last_error
                )

        client.on_message = _on_message
        client.on_disconnect = _on_disconnect

    def _start_failed(self, name: str, rt: AccountRuntime, exc: BaseException) -> None:
        """Record a start failure: status failed + last_error + AccountFailed."""
        rt.status = AccountStatus.FAILED
        rt.last_error = str(exc) or f"{type(exc).__name__}: {exc!r}"
        self._logger.error("account %s failed to start: %s", name, rt.last_error)
        self._bus.publish_nowait(AccountFailed(account=name, error=rt.last_error))

    # --------------------------------------------------------------- sending
    async def _maybe_typing(self, name: str, rt: "AccountRuntime", channel_id: int) -> None:
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
        try:
            await rt.client.send_typing(channel_id)
        except Exception:
            pass  # cosmetic best-effort only

    async def send_hook(self, task: Any) -> DiscordMessage:
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
        account = self._accounts.get(name) if isinstance(name, str) else None
        if account is None:
            raise AccountError(f"task targets unknown account {name!r}")
        rt = account.runtime
        if rt.status != AccountStatus.RUNNING or rt.client is None:
            raise ConnectionError(
                f"account {name!r} is not running (status: {rt.status.value})"
            )
        action = getattr(task, "action", None)
        client = rt.client
        if action == "dm":
            # resolution order: memory cache -> persistent api_cache -> ONE
            # client.open_dm (recorded in both, never fetched again)
            channel_id = await resolve_dm_channel(
                rt.cache, client, int(task.user_id), name, self._api_cache
            )
            # send through the RESOLVED channel: client.send_dm(user_id)
            # would re-fetch the user + re-open the DM (2 redundant REST
            # calls per send — a classic selfbot flag vector)
            try:
                message = await client.send_message(channel_id, task.content)
            except Exception as exc:
                if type(exc).__name__ == "TargetGone":
                    invalidate_dm_channel(rt.cache, self._api_cache,
                                          int(task.user_id), name)
                raise
        elif action == "message":
            rt.cache.validate_channel(int(task.channel_id))  # TargetGone propagates
            channel_id = int(task.channel_id)
            await self._maybe_typing(name, rt, channel_id)
            message = await client.send_message(channel_id, task.content)
        else:
            raise SchedulerError(
                f"unsupported action {action!r} "
                "(supported: 'message', 'dm')"
            )
        # NOTE: outbound AUDIT is written by the scheduler at task-terminal
        # (exactly-once, with attempts context) — recording here too would
        # duplicate every send in the trail (found at integration).
        return message

    # -------------------------------------------------------------- details
    def details(self, name: str) -> Dict[str, Any]:
        """Account details page (spec §8) — EXACT shape:

        ``{"name", "status": {"connected","state","latency_ms","uptime_s"},
        "identity": {"name","discord_id"}, "runtime": {"cached_guilds",
        "cached_channels","cached_dms","cache_source"}, "plugins":
        [{"name","enabled"}], "profiles": [...], "rate_limit": {...}}``

        Everything runtime-y draws ONLY from the live client's ingested cache.
        ``rate_limit`` is built only when a limiter was injected: actions come
        from ``limiter.policies()``, effect from ``limiter.effective(action,
        name)``; ``source`` is ``"inherited from global"`` when at most one
        level applies, else the comma-joined ``level:owner`` list (owner
        omitted for the global level, which has none).
        """
        account = self._get(name)
        rt = account.runtime
        self._refresh_record(account)
        record = account.record
        client = rt.client
        state = rt.status.value
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
                "name": rt.cache.user_name or record.name,
                "discord_id": rt.cache.user_id if rt.cache.user_id is not None
                else rt.user_id,
            },
            "runtime": {
                "cached_guilds": len(rt.cache.guilds),
                "cached_channels": len(rt.cache.channels),
                "cached_dms": rt.cache.dms(),
                "cache_source": "client cache (READY)",
            },
            "plugins": self._plugin_rows(account),
            "profiles": list(record.profiles),
            "rate_limit": self._rate_limit_rows(name),
        }

    def _rate_limit_rows(self, name: str) -> Dict[str, Dict[str, Any]]:
        """Effective rate policy per action (empty dict without a limiter)."""
        if self._limiter is None:
            return {}
        try:
            policies = self._limiter.policies() or []
            rows: Dict[str, Dict[str, Any]] = {}
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
                        if s.get("owner") else str(s.get("level"))
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

    def _plugin_rows(self, account: Account) -> List[Dict[str, Any]]:
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
        ``services.settings`` stays a live view.
        """
        key = (account_name, plugin_name)
        if key in self._services:
            return self._services[key]
        account = self._accounts.get(account_name)
        cache = account.runtime.cache if account is not None else DiscordCache(
            account_name
        )
        settings_dict: Dict[str, Any] = {}
        try:
            # LIVE view first (set_setting mutates this dict in place, so
            # services.settings and ctx.settings can never diverge); the
            # states() copy is only a fallback for duck-typed collaborators
            live = getattr(self._plugin_manager, "live_settings", None)
            settings_dict = live(account_name, plugin_name) if callable(live) else None
            if not isinstance(settings_dict, dict):
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
            client_getter=lambda: self._client_of(account_name),
            api_cache_repo=self._api_cache,
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
    def _attach_plugins(self, name: str) -> None:
        """attach_account with the CURRENT profile grants (None = all).

        Pinned sync; when the plugin manager nonetheless returns a coroutine
        it is scheduled as a tracked background task (no loop -> dropped).
        """
        try:
            outcome = self._plugin_manager.attach_account(
                name, self._allowed_plugins(name)
            )
            self._spawn_maybe_async(outcome, f"attach_account({name!r})")
        except Exception:
            self._logger.exception("plugin attach failed for %s", name)

    def _spawn_maybe_async(self, outcome: Any, label: str) -> None:
        """Run a collaborator's coroutine result in the background (strong ref)."""
        if not asyncio.iscoroutine(outcome):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (e.g. sync boot) — drop, don't leak
            outcome.close()
            self._logger.warning("no event loop for %s; call dropped", label)
            return
        task = loop.create_task(outcome, name=f"acct-{label}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _allowed_plugins(self, name: str) -> Optional[set]:
        """Profile-derived plugin grant (None = unrestricted)."""
        try:
            return self._profile_manager.allowed_plugins(name)
        except Exception:
            self._logger.exception(
                "could not resolve allowed plugins for %s (unrestricted)", name
            )
            return None

    def _refresh_plugins(self, account: Account) -> None:
        """Refresh Account.plugins from plugin_manager.states (keep last known)."""
        name = account.record.name
        try:
            states = self._plugin_manager.states(name)
        except Exception:
            return
        if not states:
            return  # plugin manager does not have the account (yet) — keep old
        plugins: Dict[str, Dict[str, Any]] = {}
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
                note=f"author={event.author_id}{' dm' if event.dm else ''}",
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
            raise AccountError(
                f"unknown account {name!r} (registered: {sorted(self._accounts)})"
            )
        return account

    def _repo_get(self, name: str) -> Optional[AccountRecord]:
        """Fresh record from the repository (None on failure/unknown)."""
        try:
            return self._accounts_repo.get(name)
        except Exception:
            self._logger.exception("could not read account %r from repository", name)
            return None

    def _refresh_record(self, account: Account) -> None:
        """Pull persistent state (profiles, enabled...) from the repository."""
        fresh = self._repo_get(account.record.name)
        if fresh is not None:
            account.record = fresh

    def _resolve_token(self, record: AccountRecord) -> str:
        """Token value for an account (.env lookup by convention key).

        Raises:
            CredentialsError: actionable message when the key is missing.
        """
        ref = record.effective_token_ref()
        token = self._credentials.get(ref)
        if not token:
            raise CredentialsError(
                f"account {record.name!r} has no token: set {ref!r} in .env "
                f"(or add the account with token_ref=<env key>)"
            )
        return token

    def _reconnect_settings(self, name: str) -> Dict[str, float]:
        """Account reconnect policy via SettingsManager (duck-typed, defaults
        on any failure so a settings problem never blocks reconnection)."""
        try:
            raw = self._settings.account_settings(name) or {}
        except Exception:
            raw = {}
        try:
            return {
                "enabled": bool(raw.get("reconnect_enabled", True)),
                "initial": max(0.0, float(raw.get("reconnect_initial_delay",
                                                   _DEFAULT_RECONNECT["initial"]))),
                "max": max(0.1, float(raw.get("reconnect_max_delay",
                                               _DEFAULT_RECONNECT["max"]))),
                "multiplier": max(1.0, float(raw.get("reconnect_multiplier",
                                                     _DEFAULT_RECONNECT["multiplier"]))),
            }
        except (TypeError, ValueError):
            return {"enabled": True, **_DEFAULT_RECONNECT}

    def _validate_profiles_known(self, profiles: List[str]) -> None:
        """Reject unknown profile names up front (typo trap) with the known list."""
        try:
            known = sorted(p["name"] for p in self._profile_manager.profiles())
        except Exception:
            known = []
        unknown = [p for p in profiles if p not in known]
        if unknown:
            raise ProfileError(
                f"unknown profile(s) {unknown} (known profiles: {known})"
            )


__all__ = ["Account", "AccountManager", "AccountRuntime", "AccountStatus", "NAME_RE"]
