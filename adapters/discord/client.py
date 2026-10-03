"""Real Discord client adapter (ported from the audited core/discord_adapter.py).

Bridges the :class:`adapters.base.DiscordClient` protocol to a live
user-account-capable Discord client library (e.g. ``discord.py-self``).

Ported, audit-proven behaviour:
  * the library import is LAZY (inside :meth:`RealDiscordClient.connect`) so
    tests, simulations and CLI dry-runs never require the library;
  * startup errors FAIL the connect instead of silently yielding a session
    with ``user_id=None`` (audit round 2 fix);
  * a client that reports ready without a user id is treated as a bad token;
  * ``cached_snapshot()`` builds EXACTLY from what the live client already
    holds in cache after READY — never a network fetch.

New in the rewrite:
  * library HTTP errors are translated into the shared domain vocabulary
    (:class:`core.errors.ServerRateLimit` / ``AccessDenied`` / ``TargetGone``)
    so the scheduler can react without knowing the library;
  * ``latency_ms`` and the ``Retry-After`` / ``X-RateLimit-Global`` header
    intake required by the send pipeline (INTEGRATION.md).

NOTE: selfbots violate Discord ToS; the framework keeps pacing conservative.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import math
from typing import Any, Callable, Optional

from adapters.base import DiscordMessage
from adapters.model import _emoji_repr
from adapters.model import convert_message
from core._asyncutil import outer_cancel_pending
from core.credentials import Credentials
from core.errors import (
    AccessDenied,
    AccountError,
    CredentialsError,
    ServerRateLimit,
    TargetGone,
)

LOG = logging.getLogger("slaughtermudae.adapter")

#: how long connect() waits for READY before failing (the library's own
#: connect loop retries forever — a black-holed gateway must not hang boot)
CONNECT_TIMEOUT_S = 90.0


# ------------------------------------------------- per-process headers cache
#: M3 (isolation audit S3-3.3/S7-A2): discord.py-self fetches its client
#: properties from a third-party API (cordapi.dolfi.es) on EVERY
#: HTTPClient.startup() — once per account start AND once per reconnect
#: (we build a fresh client per attempt). Under a reconnect cycle that
#: hammers cordapi (which then throttles us onto the STATIC fallback
#: fingerprint — more detectable) and adds up to 3s of latency per attempt.
#: We cache the fetched property DATA process-wide with a TTL; the Headers
#: INSTANCE stays per-client so the per-client identity UUIDs
#: (client_launch_id / client_heartbeat_session_id) are NEVER shared.
_HEADERS_CACHE_TTL = 600.0
_headers_cache: dict = {}          # (proxy, proxy_auth) -> (monotonic, result)
_headers_cache_lock: Optional[asyncio.Lock] = None
_headers_patch_installed = False


def _install_headers_fetch_cache(discord: Any, ttl: float = _HEADERS_CACHE_TTL) -> None:
    """Wrap ``Headers.get_api_properties`` with a process-wide TTL cache.

    Idempotent; silently skipped when the library layout changes (a missing
    seam must never break connect()).
    """
    global _headers_patch_installed, _headers_cache_lock
    if _headers_patch_installed:
        return
    try:
        headers_cls = discord.utils.Headers
        original = headers_cls.get_api_properties  # bound classmethod
    except AttributeError:
        return
    # FINAL-PHASE fix: never WRAP OUR OWN WRAPPER — a second install (a
    # re-import, a test re-run in-process) capturing the cached method as
    # "original" self-deadlocks on the non-reentrant lock. Unwrap to the
    # true original via the marker attribute instead.
    original = getattr(original, "_sm_original", original)
    _headers_patch_installed = True
    if _headers_cache_lock is None:
        _headers_cache_lock = asyncio.Lock()

    async def cached_get_api_properties(cls, session, type_, *, proxy=None,
                                         proxy_auth=None):
        key = (proxy, proxy_auth)
        loop = asyncio.get_running_loop()
        if _headers_cache_lock is not None:
            # FINAL-PHASE (F8 gap 5): SINGLE-FLIGHT — the fetch runs INSIDE
            # the lock (it previously ran between the read and write
            # windows, so a reconnect storm / cold fleet boot fired up to N
            # concurrent cordapi fetches — exactly the hammering the cache
            # exists to prevent). Waiters already queue on the IdentifyGate,
            # so serializing behind one ~3s fetch is harmless.
            async with _headers_cache_lock:
                hit = _headers_cache.get(key)
                if hit is not None and loop.time() - hit[0] < ttl:
                    return hit[1]
                result = await original(session, type_, proxy=proxy,
                                        proxy_auth=proxy_auth)
                _headers_cache[key] = (loop.time(), result)
                return result
        result = await original(session, type_, proxy=proxy,
                                proxy_auth=proxy_auth)
        return result

    cached_get_api_properties._sm_original = original  # unwrap marker

    try:
        headers_cls.get_api_properties = classmethod(cached_get_api_properties)
    except (AttributeError, TypeError):  # pragma: no cover - exotic layouts
        _headers_patch_installed = False


def _header(headers: Any, name: str) -> Optional[str]:
    """Case-tolerant header lookup (library header dicts vary in casing)."""
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        return value
    except Exception:
        return None


class RealDiscordClient:
    """DiscordClient protocol implementation over a lazily imported library."""

    def __repr__(self) -> str:  # FINAL-PHASE (F7): explicit redacting repr —
        # the token lives in self._token; the default object repr is safe
        # today, but a dataclass refactor or vars()-style debugging would
        # leak it into tracebacks/benchmark output.
        return (f"<RealDiscordClient account={self._account!r}"
                f" user_id={self.user_id!r} connected="
                f"{bool(getattr(self._client, 'ws', None))}>")

    def __init__(
        self,
        token: str,
        logger: Optional[logging.Logger] = None,
        identify_gate: Optional["IdentifyGate"] = None,
    ) -> None:
        self._token = token
        self._discord: Any = None          # the discord module (set in connect)
        self._client: Any = None           # the live Client instance
        self._ready = asyncio.Event()
        self._startup_error: Optional[Exception] = None
        self._connect_aborted = False  # stop()/disconnect raced a pending connect
        self._zombie_tasks: set = set()  # un-reaped run tasks (strong refs)
        self._run_task: Optional["asyncio.Task"] = None  # strong ref (see connect)
        self._logger = logger or LOG
        self._identify_gate = identify_gate  # shared per process (factory-owned)
        #: best-effort account label for domain error messages (set by factory)
        self._account = ""
        #: callbacks assigned by the account manager BEFORE connect()
        self.on_message: Optional[Callable[[DiscordMessage], None]] = None
        self.on_disconnect: Optional[Callable[[str], None]] = None
        # declared here so the adapter satisfies its own protocol contract
        # (adapters/base.py) even when wired by someone other than the
        # account manager — reading an undeclared attr would raise inside
        # library dispatch, which logs-and-swallows => silently dropped
        # edit/delete/reaction events (isolation audit K17)
        self.on_message_edit: Optional[Callable[..., None]] = None
        self.on_message_delete: Optional[Callable[..., None]] = None
        self.on_reaction: Optional[Callable[..., None]] = None
        self.user_id: Optional[int] = None

    # ------------------------------------------------------------ labelling
    @property
    def account(self) -> str:
        """Best-effort account name (used to enrich AccessDenied reasons)."""
        return self._account

    @account.setter
    def account(self, name: str) -> None:
        self._account = str(name or "")

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        """Import the library, wire callbacks, start the client, await ready.

        Raises:
            AccountError: library missing, client failed to start, or the
                client connected without a user id (bad token).
        """
        try:
            import discord  # type: ignore[import-untyped]  # discord.py-self fork
        except ImportError as exc:
            raise AccountError(
                "live mode requires a user-account-capable discord client "
                "library (e.g. pip install discord.py-self)"
            ) from exc
        self._discord = discord
        # discord.py-self 2.x removed Intents (user accounts don't use them);
        # construct plain and fall back cleanly across fork variants
        try:
            self._client = discord.Client()
        except TypeError:
            self._client = discord.Client(intents=None)
        if self._identify_gate is not None:
            # K8: shadowing the instance method is the library-sanctioned
            # extension point (see Client.before_identify_hook) — every
            # IDENTIFY this client sends goes through the shared per-IP gate.
            self._client.before_identify_hook = self._identify_gate.acquire
        _install_headers_fetch_cache(discord)
        self._ready = asyncio.Event()
        self._startup_error = None

        @self._client.event
        async def on_ready() -> None:
            self.user_id = self._client.user.id
            self._ready.set()

        @self._client.event
        async def on_error(event_method: str, *args: Any) -> None:
            # without this, handler exceptions die on the library's own
            # logger (raw stderr) — invisible to LogService/the dashboard
            try:
                import traceback
                self._logger.error(
                    "discord event handler %s failed: %s",
                    event_method, traceback.format_exception_only(*sys.exc_info()[:2])[-1].strip(),
                )
                self._logger.debug("handler traceback:", exc_info=True)
            except Exception:
                pass

        @self._client.event
        async def on_disconnect() -> None:
            if self.on_disconnect is not None:
                # M1/S2-M1 (observability): surface the gateway close code so
                # "gateway closed" can be told apart (1000 = server-initiated
                # close, 4007/4015-style = session limits, None = abnormal).
                # discord.py-self only sets ws._close_code when IT closes;
                # a remote CLOSE frame lands on the underlying socket —
                # check that too or Discord-side culls show no code at all.
                ws = getattr(self._client, "ws", None)
                code = getattr(ws, "close_code", None)
                if code is None:
                    code = getattr(ws, "_close_code", None)
                if code is None:
                    code = getattr(getattr(ws, "socket", None),
                                   "close_code", None)
                reason = ("gateway closed" if code is None
                          else f"gateway closed (code={code})")
                self.on_disconnect(reason)

        @self._client.event
        async def on_message(message: Any) -> None:
            if self.on_message is None or message.author.id == self.user_id:
                return
            # RICH BOUNDARY: capture everything the event already holds
            # (embeds/components/attachments/reactions/reference/author/
            # channel/guild/created_at + the runtime-only raw handle) in ONE
            # non-blocking conversion — plugins never re-fetch it (§15)
            self.on_message(
                convert_message(
                    message,
                    dm=isinstance(message.channel, self._discord.DMChannel),
                    viewer_id=self.user_id,
                )
            )

        @self._client.event
        async def on_message_edit(before: Any, after: Any) -> None:
            if self.on_message_edit is not None and after.author.id != self.user_id:
                self.on_message_edit(convert_message(
                    after, dm=isinstance(after.channel, self._discord.DMChannel)))

        @self._client.event
        async def on_message_delete(message: Any) -> None:
            if self.on_message_delete is not None and message.author.id != self.user_id:
                guild = getattr(message, "guild", None)
                self.on_message_delete(
                    message.id, message.channel.id,
                    getattr(guild, "id", None) if guild else None, message,
                )

        @self._client.event
        async def on_reaction_add(reaction: Any, user: Any) -> None:
            if self.on_reaction is not None and user.id != self.user_id:
                message = reaction.message
                guild = (getattr(message, "guild", None)
                         or getattr(getattr(message, "channel", None), "guild", None))
                self.on_reaction(
                    "add", message.id, message.channel.id,
                    getattr(guild, "id", None) if guild else None,
                    user.id, _emoji_repr(reaction.emoji), (reaction, user),
                )

        @self._client.event
        async def on_reaction_remove(reaction: Any, user: Any) -> None:
            if self.on_reaction is not None and user.id != self.user_id:
                message = reaction.message
                guild = (getattr(message, "guild", None)
                         or getattr(getattr(message, "channel", None), "guild", None))
                self.on_reaction(
                    "remove", message.id, message.channel.id,
                    getattr(guild, "id", None) if guild else None,
                    user.id, _emoji_repr(reaction.emoji), (reaction, user),
                )

        # keep a STRONG reference: the loop only weak-references tasks, so
        # an unreferenced client task can be garbage-collected mid-run, and
        # disconnect() needs the handle to cancel it
        self._run_task = asyncio.get_running_loop().create_task(self._run())
        try:
            # bounded ready wait: the library's connect loop retries forever
            # (a black-holed gateway would hang start()/--start-all boot)
            await asyncio.wait_for(self._ready.wait(), timeout=CONNECT_TIMEOUT_S)
            # AUDIT FIX (ported): startup failures must FAIL the connect
            # instead of silently yielding a session with user_id=None
            if self._startup_error is not None:
                raise AccountError(
                    f"live client failed to start: {self._startup_error}")
            if self.user_id is None:
                if self._connect_aborted:  # stop()/disconnect raced us:
                    raise ConnectionError(  # NOT an auth-shaped failure
                        "live client connect aborted by a concurrent stop")
                raise AccountError(
                    "live client connected without a user (bad token?)")
        except asyncio.TimeoutError:
            await self._cancel_run_task()
            await self._close_library_client()
            raise AccountError(
                f"live client did not become ready within "
                f"{CONNECT_TIMEOUT_S:.0f}s (network down or gateway unreachable)"
            ) from None
        except BaseException:
            # K7 (isolation audit S10-C5): a cancelled/failed connect must
            # close the LIBRARY client too — reaping only the _run task
            # leaves the aiohttp/curl sessions (and any half-open gateway)
            # unreaped; Client.close() is the sanctioned teardown.
            try:
                await self._client.close()
            except Exception:  # half-open clients can throw on close
                pass
            await self._cancel_run_task()  # no orphaned client task
            await self._close_library_client()  # aiohttp/curl session leak
            raise
        self._connect_aborted = False
        self._logger.info("live client connected as user %s", self.user_id)

    async def _close_library_client(self) -> None:
        """Close the library client's HTTP sessions (best-effort)."""
        client = self._client
        if client is None:
            return
        try:
            await client.close()
        except Exception as exc:
            self._logger.debug("library client close raised %s", exc)

    async def _run(self) -> None:
        """Run the client until it exits; capture startup errors for connect()."""
        try:
            await self._client.start(self._token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # startup failures must surface via connect()
            self._logger.error("live client terminated: %s", exc)
            self._startup_error = exc
        finally:
            self._ready.set()

    async def _cancel_run_task(self) -> None:
        """Cancel + reap the background client task (idempotent)."""
        task, self._run_task = self._run_task, None
        if task is None or task.done():
            return
        self._connect_aborted = True  # connect() must not misread this as
        task.cancel()  # a bad token (auth-shaped AccountError)
        try:
            await asyncio.wait_for(asyncio.shield(task), 2.0)
        except asyncio.CancelledError:
            if outer_cancel_pending():
                raise  # OUR caller was cancelled: never eat it
        except (asyncio.TimeoutError, Exception):
            # reap window exhausted: keep a strong ref so the still-running
            # task cannot be garbage-collected mid-flight
            self._zombie_tasks.add(task)
            task.add_done_callback(self._zombie_tasks.discard)

    async def disconnect(self) -> None:
        """Close the underlying client; safe when never started."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as exc:  # closing a half-open client can throw
                self._logger.warning("live client close raised %s", exc)
        await self._cancel_run_task()

    # ------------------------------------------------------------- messaging
    def _require_client(self) -> Any:
        """The live client, or ConnectionError when never connected."""
        if self._client is None:
            raise ConnectionError("live client is not connected")
        return self._client

    def _map_send_error(self, exc: Exception, channel_id: int) -> Optional[Exception]:
        # transient server faults retry like network errors: the library
        # already retried internally before surfacing DiscordServerError
        if type(exc).__name__ == "DiscordServerError" or (
            getattr(exc, "status", None) is not None
            and getattr(exc, "status", 0) >= 500
        ):
            return ConnectionError(f"discord server error ({exc})")
        """Translate one library send error into a domain error (or None).

        * HTTPException 429 -> ServerRateLimit (Retry-After float-parsable
          header when present, else 1.0; global flag from X-RateLimit-Global)
        * Forbidden        -> AccessDenied(account, reason)
        * NotFound         -> TargetGone("", reason)
        """
        discord = self._discord
        if discord is None or not isinstance(exc, discord.HTTPException):
            return None
        status = getattr(exc, "status", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status", None)
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if status == 429:
            raw = _header(headers, "Retry-After")
            try:
                retry_after = float(raw)
            except (TypeError, ValueError):
                retry_after = 1.0
            global_ = str(_header(headers, "X-RateLimit-Global") or "").lower() == "true"
            return ServerRateLimit(retry_after, global_)
        if status == 403 or isinstance(exc, getattr(discord, "Forbidden", ())):
            return AccessDenied(
                self._account or "",
                f"discord refused a send to channel {channel_id}: {exc}",
            )
        if status == 404 or isinstance(exc, getattr(discord, "NotFound", ())):
            return TargetGone("", f"channel {channel_id} is gone: {exc}")
        return None

    async def _send_to_channel(
        self, channel: Any, channel_id: int, content: str, *, dm: bool
    ) -> DiscordMessage:
        """Send via a resolved channel object with domain-error mapping."""
        try:
            sent = await channel.send(content)
        except Exception as exc:
            mapped = self._map_send_error(exc, channel_id)
            if mapped is not None:
                raise mapped from exc
            raise
        return DiscordMessage(
            id=getattr(sent, "id", 0),
            channel_id=channel_id,
            author_id=self.user_id or 0,
            content=content,
            # derive from the channel TYPE when possible: DM sends now arrive
            # through send_message with an already-resolved DM channel id
            dm=dm or self._is_dm_channel(channel),
        )

    def _is_dm_channel(self, channel: Any) -> bool:
        """True when the channel object is a DM channel (type-based)."""
        discord = self._discord
        if discord is None or channel is None:
            return False
        dm_type = getattr(discord, "DMChannel", None)
        return dm_type is not None and isinstance(channel, dm_type)

    async def send_message(self, channel_id: int, content: str) -> DiscordMessage:
        """Send to a channel id (cache first, fetch fallback); returns the
        framework message shape.

        Raises ServerRateLimit / AccessDenied / TargetGone for the mapped
        library failures; anything else propagates unchanged.
        """
        client = self._require_client()
        channel = client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(channel_id)
            except Exception as exc:
                mapped = self._map_send_error(exc, channel_id)
                if mapped is not None:
                    raise mapped from exc
                raise
        return await self._send_to_channel(channel, channel_id, content, dm=False)

    async def _dm_channel(self, user_id: int) -> Any:
        """Resolve (opening if needed) the DM channel object for a user."""
        client = self._require_client()
        try:
            user = await client.fetch_user(user_id)
            return user.dm_channel or await user.create_dm()
        except Exception as exc:
            mapped = self._map_send_error(exc, user_id)
            if mapped is not None:
                raise mapped from exc
            raise

    async def send_dm(self, user_id: int, content: str) -> DiscordMessage:
        """Send a DM to a user id (same error mapping as send_message)."""
        channel = await self._dm_channel(user_id)
        return await self._send_to_channel(channel, channel.id, content, dm=True)

    async def open_dm(self, user_id: int) -> int:
        """Open (or reuse) the DM channel with a user; returns its id."""
        channel = await self._dm_channel(user_id)
        return channel.id

    async def send_typing(self, channel_id: int) -> None:
        """Fire a typing indicator in a channel (anti-flag human behavior).

        Best-effort by contract: callers swallow failures — typing must
        never block or fail a send.
        """
        client = self._require_client()
        channel = client.get_channel(channel_id)
        if channel is None:
            return  # unknown channel: skip rather than fetch (API-call trim)
        try:
            await channel.typing()  # 2.x fork API (trigger_typing is gone)
        except AttributeError:
            await channel.trigger_typing()  # older forks

    async def _resolve_message(self, channel_id: int, message_id: int) -> Any:
        """Message object by ids — cache first, fetch on miss (the ONE
        plugin-demanded fetch for button/reaction actions; §15)."""
        client = self._require_client()
        channel = client.get_channel(channel_id)
        if channel is None:
            channel = await client.fetch_channel(channel_id)
        message = channel.get_message(message_id) if hasattr(
            channel, "get_message") else None
        if message is None:
            message = await channel.fetch_message(message_id)
        return message

    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> Any:
        """React to a message (kakerareact capability; pipeline-paced)."""
        try:
            message = await self._resolve_message(channel_id, message_id)
            await message.add_reaction(emoji)
        except Exception as exc:
            mapped = self._map_send_error(exc, channel_id)
            if mapped is not None:
                raise mapped from exc
            raise

    async def click_button(self, channel_id: int, message_id: int, custom_id: str) -> Any:
        """Click a component by custom_id (kakeraloot capability).

        Uses the library's Button.click() (supported on user tokens). The
        component must still exist on the live message; a vanished custom_id
        raises TargetGone with the id in the reason.
        """
        try:
            message = await self._resolve_message(channel_id, message_id)
        except Exception as exc:
            mapped = self._map_send_error(exc, channel_id)
            if mapped is not None:
                raise mapped from exc
            raise
        for top in getattr(message, "components", None) or ():
            for component in getattr(top, "children", None) or ():
                if getattr(component, "custom_id", None) == custom_id:
                    try:
                        return await component.click()
                    except Exception as exc:
                        mapped = self._map_send_error(exc, channel_id)
                        if mapped is not None:
                            raise mapped from exc
                        raise
        from core.errors import TargetGone

        raise TargetGone("", f"custom_id {custom_id!r} not on message {message_id}")

    async def fetch_user(self, user_id: int) -> Any:
        """Fetch a user object; None when the user is gone (NotFound).

        The services seam (resolve_user) documents None-on-unresolvable —
        letting NotFound propagate violated that contract live.
        """
        try:
            return await self._require_client().fetch_user(user_id)
        except Exception as exc:
            if type(exc).__name__ == "NotFound":
                return None
            raise

    # ---------------------------------------------------------- observability
    @property
    def latency_ms(self) -> Optional[float]:
        """Gateway heartbeat latency in ms (None before READY / when NaN)."""
        client = self._client
        if client is None:
            return None
        try:
            latency = client.latency
        except Exception:
            return None
        if latency is None:
            return None
        try:
            latency = float(latency)
        except (TypeError, ValueError):
            return None
        if math.isnan(latency) or latency < 0:
            return None
        return latency * 1000.0

    def cached_snapshot(self) -> dict:
        """In-memory view only — NO network fetches (details page source).

        Uses exactly what the live client already holds in cache after READY:
        our user, guilds + their channels, open DM channels (recipient ids),
        and the heartbeat latency. ``{"available": False}`` before connect.
        """
        client = self._client
        if client is None or self.user_id is None:
            return {"available": False}
        user = client.user
        guilds = [
            {
                "id": guild.id,
                "name": guild.name,
                "channels": [
                    {"id": channel.id, "name": getattr(channel, "name", None) or ""}
                    for channel in getattr(guild, "channels", [])
                ],
            }
            for guild in getattr(client, "guilds", [])
        ]
        dms = [
            {
                "id": channel.id,
                "recipient": getattr(getattr(channel, "recipient", None), "id", None),
            }
            for channel in getattr(client, "private_channels", [])
        ]
        return {
            "available": True,
            "user": {"id": user.id, "name": str(user)},
            "guilds": guilds,
            "dm_channels": dms,
            "latency_ms": self.latency_ms,
        }


class IdentifyGate:
    """Fleet-wide IDENTIFY pacer (Discord's identify limit is per-IP).

    discord.py-self 2.1.0 ships NO identify serializer — the library's
    ``before_identify_hook`` is a no-op by default — so N clients in one
    process can IDENTIFY simultaneously and trip Discord's per-IP gateway
    connection limit (observed live as ``Connection reset by peer`` during
    startup and, worse, during synchronized reconnect storms that
    ``start_all``'s stagger does not cover). One gate instance is shared
    by every client the factory builds; waiters are released FIFO with
    at least ``interval`` seconds between consecutive IDENTIFYs
    (isolation audit K8 / S3-3.1, S7-A2).
    """

    __slots__ = ("interval", "max_wait", "_lock", "_last", "_depth")

    def __init__(self, interval: float = 5.0,
                 max_wait: float = 45.0) -> None:
        self.interval = float(interval)
        #: fast-fail bound: 45s >= 8 accounts' worth of queue at 5s spacing
        self.max_wait = float(max_wait)
        self._lock: Optional[asyncio.Lock] = None  # bound lazily to the loop
        self._last: float = float("-inf")
        self._depth: int = 0  # FINAL-PHASE (F4): FIFO waiters ahead-of-lock

    async def acquire(self, shard_id=None, *, initial: bool = False) -> None:
        """Space consecutive IDENTIFYs by ``interval`` seconds.

        The leading ``shard_id`` positional is tolerated (never used —
        user accounts are unsharded — but discord.py lineage hooks are
        invoked with it positionally, and library drift must not turn
        the gate into a TypeError). Fast-fails with a retryable
        ConnectionError when the PROJECTED total wait (residual spacing
        + waiters already queued ahead × interval) exceeds ``max_wait``
        (FINAL-PHASE F4: the phase-4 fast-fail checked the residual only
        AFTER lock acquisition — where it always equals ≤ interval — so
        it could never fire; queue depth tracking makes the projection
        real). Failing fast into the reconnect backoff is strictly
        cheaper than parking a built client for 90s only to time out.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        # projected wait BEFORE queueing: residual + the queue depth ahead
        # of us each costs a full interval (FIFO under the held lock)
        loop = asyncio.get_running_loop()
        projected = (self._last + self.interval * (self._depth + 1)
                     - loop.time()) if self._last != float("-inf") else 0.0
        if projected > self.max_wait:
            raise ConnectionError(
                f"identify gate saturated: projected wait {projected:.0f}s "
                f"exceeds {self.max_wait:.0f}s ({self._depth} queued) — "
                "retrying with backoff is cheaper than parking a built client")
        self._depth += 1
        try:
            async with self._lock:  # FIFO: hold while sleeping so waiters queue
                now = loop.time()
                wait = self._last + self.interval - now
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = loop.time()
                self._last = now
        finally:
            self._depth -= 1


class RealClientFactory:
    """ClientFactory protocol implementation building live clients.

    The pinned factory signature is ``(name, token)`` (adapters/base.py):
    the AccountManager resolves the token EXCLUSIVELY from the encrypted
    CredentialService (database) and hands the decrypted value over.
    No .env fallback exists — an empty token means MISSING_CREDENTIAL
    upstream, surfaced as a CredentialsError here.
    """

    def __init__(
        self,
        credentials: Optional[Credentials] = None,
        logger: Optional[logging.Logger] = None,
        identify_interval: float = 5.0,
    ) -> None:
        self._credentials = credentials  # kept for metadata only (mudae id, owner ids)
        self._logger = logger or LOG
        # ONE gate per process (the factory is built once in bootstrap):
        # every client this factory produces IDENTIFYs through it.
        self._identify_gate = IdentifyGate(identify_interval)

    def __call__(self, name: str, token: str) -> RealDiscordClient:
        """Build a RealDiscordClient for one account.

        Raises CredentialsError when no token is available (the caller's
        credential resolution failed — dashboard: Accounts → credential…).
        """
        if not token:
            raise CredentialsError(
                f"account {name!r} has no stored credential — add one "
                "through the dashboard or CLI 'accounts credential'"
            )
        # K13 (S9-C1): a per-account child logger keeps adapter lines
        # (connected/terminated/close-failed) attributable — the bridge
        # maps slaughtermudae.adapter.<name> to source "adapter:<name>".
        logger = self._logger.getChild(name) if name else self._logger
        client = RealDiscordClient(
            token=token, logger=logger, identify_gate=self._identify_gate)
        client.account = name  # enrich AccessDenied reasons, best effort
        return client


__all__ = ["IdentifyGate", "RealDiscordClient", "RealClientFactory"]
