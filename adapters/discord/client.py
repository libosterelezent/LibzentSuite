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
import base64
import copy
import json
import logging
import math
import sys
import time
import uuid
from typing import Any, Callable, FrozenSet, Optional

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
from core.plugin_manager.commands import COMMAND_HUB

LOG = logging.getLogger("slaughtermudae.adapter")

#: how long connect() waits for READY before failing (the library's own
#: connect loop retries forever — a black-holed gateway must not hang boot)
CONNECT_TIMEOUT_S = 90.0

#: round-3 F4-1: hard ceiling handed to the library for in-request 429
#: sleeps. UNSET (None — the default, client.py:377) makes HTTPClient sleep
#: ANY ``retry_after`` inside the request (http.py:991-1018) and pre-request
#: bucket sleeps (http.py:521-546), so one pathological 429 could park a
#: scheduler slot for minutes while the adapter's RateLimited mapping stays
#: dead code (it only fires when the cap is set). 30.0 is the fork's own
#: minimum clamp: http.py:637 ``max(30.0, max_ratelimit_timeout)``.
MAX_RATELIMIT_TIMEOUT_S = 30.0

#: round-3 F4-6: the fork's interaction path retries 202s with code
#: 110000-119999 by sleeping ``retry_after`` with NO cap and exempt from
#: max_ratelimit_timeout (http.py:936-948); _wrapped_interaction also waits
#: on the gateway (interactions.py:210-214). Bound one click's total await.
CLICK_TIMEOUT_S = 30.0


# ------------------------------------------------- per-process headers cache
#: M3 (isolation audit S3-3.3/S7-A2): discord.py-self fetches its client
#: properties from a third-party API (cordapi.dolfi.es) on EVERY
#: HTTPClient.startup() — once per account start AND once per reconnect
#: (we build a fresh client per attempt). Under a reconnect cycle that
#: hammers cordapi (which then throttles us onto the STATIC fallback
#: fingerprint — more detectable) and adds up to 3s of latency per attempt.
#: We cache the fetched property DATA process-wide with a TTL; the Headers
#: INSTANCE stays per-client. Round-3 F4-7: every cache HIT is deep-copied
#: and its per-client identity UUIDs (client_launch_id /
#: client_heartbeat_session_id — regenerated per request by the fork's own
#: fallback path, utils.py:1804-1806) are re-rolled so two accounts can
#: never share one fingerprint; the encoded super-properties header is
#: re-encoded to stay consistent with the fresh UUIDs.
_HEADERS_CACHE_TTL = 600.0
#: keys in super_properties that identify ONE client instance (fork:
#: utils.py:1804-1806)
_HEADERS_IDENTITY_KEYS = ("client_launch_id", "client_heartbeat_session_id")
_headers_cache: dict = {}          # (proxy, proxy_auth) -> (monotonic, result)
_headers_cache_lock: Optional[asyncio.Lock] = None
_headers_cache_loop: Any = None    # the loop _headers_cache_lock is bound to
_headers_patch_installed = False


def _headers_lock_for(loop: Any) -> asyncio.Lock:
    """A cache lock bound to the CURRENT loop (round-3 F4-7).

    An asyncio.Lock binds to the first loop that uses it; a module-level
    lock reused from a second ``asyncio.run`` raises RuntimeError inside
    connect(). Recreate the primitive whenever the running loop changes.
    """
    global _headers_cache_lock, _headers_cache_loop
    if _headers_cache_lock is None or _headers_cache_loop is not loop:
        _headers_cache_lock = asyncio.Lock()
        _headers_cache_loop = loop
    return _headers_cache_lock


def _encode_super_properties(properties: dict, fallback: str,
                             discord: Any) -> str:
    """b64(JSON) exactly like the fork (utils.py:1813 b64encode(_to_json)).

    Falls back to the cached encoding when anything unexpected happens —
    a re-encode failure must never break connect().
    """
    to_json = getattr(getattr(discord, "utils", None), "_to_json", None)
    if to_json is None:
        def to_json(obj: Any) -> str:  # type: ignore[misc]
            return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)
    try:
        return base64.b64encode(
            to_json(properties).encode("utf-8")).decode("utf-8")
    except Exception:
        return fallback


def _reidentified_copy(result: tuple, discord: Any) -> tuple:
    """Deep-copy a cached headers result + re-roll per-client identity.

    The stored dicts are NEVER handed to a second account as-is (fork
    parity: the fallback path regenerates these UUIDs per request,
    utils.py:1804-1806). Non-deepcopyable payloads pass through unchanged
    rather than failing the caller.
    """
    try:
        properties, extra, encoded = result
        properties = copy.deepcopy(properties)
        extra = copy.deepcopy(extra)
    except Exception:
        return result
    fresh = False
    if isinstance(properties, dict):
        for key in _HEADERS_IDENTITY_KEYS:
            if key in properties:
                properties[key] = str(uuid.uuid4())
                fresh = True
    if fresh:
        encoded = _encode_super_properties(properties, encoded, discord)
    return (properties, extra, encoded)


def _install_headers_fetch_cache(discord: Any, ttl: float = _HEADERS_CACHE_TTL) -> None:
    """Wrap ``Headers.get_api_properties`` with a process-wide TTL cache.

    Idempotent; silently skipped when the library layout changes (a missing
    seam must never break connect()).
    """
    global _headers_patch_installed
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

    async def cached_get_api_properties(cls, session, type_, *, proxy=None,
                                         proxy_auth=None):
        key = (proxy, proxy_auth)
        loop = asyncio.get_running_loop()
        try:
            lock = _headers_lock_for(loop)
        except RuntimeError:
            lock = None  # no running loop: bypass the cache entirely
        if lock is not None:
            # FINAL-PHASE (F8 gap 5): SINGLE-FLIGHT — the fetch runs INSIDE
            # the lock (it previously ran between the read and write
            # windows, so a reconnect storm / cold fleet boot fired up to N
            # concurrent cordapi fetches — exactly the hammering the cache
            # exists to prevent). Waiters already queue on the IdentifyGate,
            # so serializing behind one ~3s fetch is harmless.
            # Round-3: the lock is PER-LOOP (a process-global lock binds to
            # the first loop and breaks every later one) and hits are DEEP
            # COPIES with re-rolled per-client identity UUIDs — a shared
            # frozen properties dict would fingerprint-correlate the whole
            # fleet for the cache TTL.
            async with lock:
                hit = _headers_cache.get(key)
                if hit is not None and loop.time() - hit[0] < ttl:
                    return _reidentified_copy(hit[1], discord)
                result = await original(session, type_, proxy=proxy,
                                        proxy_auth=proxy_auth)
                _headers_cache[key] = (
                    loop.time(), _reidentified_copy(result, discord))
                return result
        result = await original(session, type_, proxy=proxy,
                                proxy_auth=proxy_auth)
        return result

    cached_get_api_properties._sm_original = original  # unwrap marker

    try:
        headers_cls.get_api_properties = classmethod(cached_get_api_properties)
    except (AttributeError, TypeError):  # pragma: no cover - exotic layouts
        _headers_patch_installed = False


_MESSAGE_DEDUP_WINDOW_S = 120.0
_MESSAGE_DEDUP_MAX = 512

#: round-3 F4-3: the fork dispatches BOTH the raw_* payload event and the
#: cached-only rich event for cached messages, raw FIRST (state.py:1848→1850
#: delete, 1877→1882 edit, 1927→1937 reaction). The raw handler publishes
#: and marks a key; the rich handler suppresses itself when the key was
#: marked inside this window (same gateway dispatch, one loop turn apart —
#: 5s is generous and never suppresses distinct rapid gateway bursts).
_RICH_EVENT_DEDUP_WINDOW_S = 5.0

#: ChannelType names (lowercased) that count as TEXT channels for the
#: picker snapshot: regular text + announcement/news channels (both are
#: message surfaces); categories/voice/stage/forum are skipped.
_TEXT_CHANNEL_TYPES = ("text", "news")


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
        command_prefix: Optional[str] = None,
        owner_ids: FrozenSet[int] = frozenset(),
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
        #: self-command prefix ("" = disabled; "@me" = when_mentioned).
        #: When set, the adapter builds an ext.commands Bot in self_bot
        #: mode: the account's OWN messages become invocable commands,
        #: other users' messages are skipped by the library's check.
        self._command_prefix = command_prefix or ""
        #: owner gate for self-commands (OWNER_USER_IDS from .env, threaded
        #: through the factory's credentials): the account's own Discord
        #: user id must be an owner — exactly the trigger-word path's gate.
        self._owner_ids = frozenset(owner_ids or ())
        self._identify_gate = identify_gate  # shared per process (factory-owned)
        #: best-effort account label for domain error messages (set by factory)
        self._account = ""
        #: self-command registry state: name -> spec (latest published set)
        #: and the names currently registered on the Bot (rebuild diffing)
        self._self_command_specs: dict = {}
        self._self_command_names: set = set()
        #: wall-clock anchor for the `pg` uptime stat (set at READY)
        self._connected_since: Optional[float] = None
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
        #: session liveness (SimClient parity): True only between a
        #: successful connect() and the terminal exit of the client run /
        #: disconnect() — gateway drops do NOT flip it (the library
        #: auto-resumes and keeps accepting REST traffic)
        self.connected: bool = False
        self._seen_messages: dict = {}
        #: raw/rich double-dispatch suppression keys (see
        #: _RICH_EVENT_DEDUP_WINDOW_S) — key -> loop-monotonic timestamp
        self._seen_event_keys: dict = {}

    def _message_already_seen(self, message_id: Any) -> bool:
        """True when this inbound id arrived recently (gateway RESUME replay).

        The window is per client instance on purpose: the account manager
        builds a FRESH client per reconnect (new IDENTIFY, new session —
        no replay), while the library's own auto-resume happens inside ONE
        client instance where Discord re-delivers unacked dispatches.
        """
        if message_id is None:
            return False
        now = asyncio.get_running_loop().time()
        seen = self._seen_messages
        if len(seen) >= _MESSAGE_DEDUP_MAX:
            cutoff = now - _MESSAGE_DEDUP_WINDOW_S
            for mid in [m for m, t in seen.items() if t < cutoff]:
                del seen[mid]
            if len(seen) >= _MESSAGE_DEDUP_MAX:
                seen.clear()
        if message_id in seen:
            seen[message_id] = now
            return True
        seen[message_id] = now
        return False

    def _mark_event_key(self, key: tuple) -> bool:
        """Record an event key; True when the SAME key was already recorded
        inside the dedup window (raw/rich double-dispatch suppression).

        Call AFTER a successful publish: a callback that raised leaves the
        key unmarked so the sibling handler can still deliver the event.
        """
        now = asyncio.get_running_loop().time()
        seen = self._seen_event_keys
        if len(seen) >= _MESSAGE_DEDUP_MAX:
            cutoff = now - _RICH_EVENT_DEDUP_WINDOW_S
            for stale in [k for k, t in seen.items() if t < cutoff]:
                del seen[stale]
        previous = seen.get(key)
        seen[key] = now
        return previous is not None and now - previous < _RICH_EVENT_DEDUP_WINDOW_S

    def _publish_raw_reaction(self, kind: str, payload: Any) -> None:
        """RawReactionActionEvent -> the on_reaction callback shape.

        Fires for EVERY reaction dispatch (cached or not — state.py:1927
        dispatches raw_reaction_add unconditionally, 1937 gates the rich
        one on the message cache), which is what makes kakera reactions on
        old/restart-surviving messages visible at all.
        """
        if self.on_reaction is None:
            return
        user_id = getattr(payload, "user_id", None)
        if user_id is None or user_id == self.user_id:
            return
        emoji_text = _emoji_repr(getattr(payload, "emoji", None))
        self.on_reaction(
            kind,
            getattr(payload, "message_id", None),
            getattr(payload, "channel_id", None),
            getattr(payload, "guild_id", None),
            user_id, emoji_text, payload,
        )
        self._mark_event_key(
            ("reaction", kind, getattr(payload, "message_id", None),
             user_id, emoji_text))

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
        # discord.py-self 2.x removed Intents (user accounts don't use
        # them) — construct plainly (round-3 F4-8: the old intents=None
        # fallback was dead code; the kwarg is silently swallowed by
        # ConnectionState's **options sink). max_ratelimit_timeout is
        # explicit (round-3 F4-1): unset, the library sleeps ANY 429
        # retry_after in-request (http.py:991-1018) and our
        # RateLimited→ServerRateLimit mapping can never fire — the pinned
        # discord.py-self 2.1.0 accepts the kwarg (min-clamped at 30 by
        # http.py:637); the TypeError fallback covers only exotic/test
        # layouts without it.
        # SELF-COMMAND layer (upstream): with a prefix configured,
        # construct the ext.commands Bot (user_bot=True) instead of a
        # plain Client. user_bot is the correct mode for this framework:
        # ANY owner in OWNER_USER_IDS may command the account (the
        # multi-account model — one owner typing, every framework account
        # responding), whereas self_bot=True would dispatch only the
        # account's OWN messages. user_bot's _skip_check lets every
        # message reach dispatch, so ownership is enforced by OUR
        # invoker gate (allowed_invoker) — fail-closed. With no prefix,
        # behavior is exactly today's (plain Client, no command
        # machinery).
        if self._command_prefix:
            from discord.ext import commands as _ext_commands
            prefix = self._command_prefix
            if prefix == "@me":
                prefix = _ext_commands.when_mentioned
            try:
                self._client = _ext_commands.Bot(
                    command_prefix=prefix, user_bot=True,
                    owner_ids=set(self._owner_ids),
                    help_command=None,
                    max_ratelimit_timeout=MAX_RATELIMIT_TIMEOUT_S,
                )
            except TypeError:
                self._logger.warning(
                    "discord client library ignores max_ratelimit_timeout — "
                    "in-request 429 sleeps are UNBOUNDED on this layout")
                self._client = _ext_commands.Bot(
                    command_prefix=prefix, user_bot=True,
                    owner_ids=set(self._owner_ids),
                    help_command=None,
                )
        else:
            try:
                self._client = discord.Client(
                    max_ratelimit_timeout=MAX_RATELIMIT_TIMEOUT_S)
            except TypeError:
                self._logger.warning(
                    "discord client library ignores max_ratelimit_timeout — "
                    "in-request 429 sleeps are UNBOUNDED on this layout")
                self._client = discord.Client()
        if self._identify_gate is not None:
            # K8: shadowing the instance method is the library-sanctioned
            # extension point (see Client.before_identify_hook) — every
            # IDENTIFY this client sends goes through the shared per-IP gate.
            self._client.before_identify_hook = self._identify_gate.acquire
        _install_headers_fetch_cache(discord)
        # SELF-COMMAND layer: when a prefix is configured, use the
        # library's ext.commands Bot in user_bot mode — ANY owner from
        # OWNER_USER_IDS (including a different framework account's
        # user) can invoke commands on this account; the adapter's
        # allowed_invoker gate enforces ownership fail-closed (non-owners
        # get no reply at all). The plugin EVENT path keeps the
        # self-echo filter below: a command never becomes a plugin
        # event, and a plugin's own output never re-triggers anything.
        if self._command_prefix:
            self._install_command_layer(discord)
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
            # SELF-COMMAND path: when the command layer is active, route
            # every message through process_commands — user_bot mode lets
            # the library hand EVERY message to dispatch (its _skip_check
            # never skips), and ownership is enforced per-invocation by
            # _dispatch_self_command's allowed_invoker gate, so random
            # users can never fire a command (fail-closed).
            # This runs BEFORE the self-echo filter: commands and events
            # are SEPARATE paths (a command never becomes a plugin event,
            # and the account's own output never re-triggers plugins).
            if self._command_prefix:
                try:
                    await self._client.process_commands(message)
                except Exception:
                    self._logger.debug(
                        "self-command dispatch failed", exc_info=True)
            if self.on_message is None or message.author.id == self.user_id:
                return
            if self._message_already_seen(message.id):
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
            if self.on_message_edit is None or after.author.id != self.user_id:
                return
            if self._mark_event_key(("edit", after.id)):
                return  # the raw handler already published this dispatch
            self.on_message_edit(convert_message(
                after, dm=isinstance(after.channel, self._discord.DMChannel)))

        @self._client.event
        async def on_message_delete(message: Any) -> None:
            if self.on_message_delete is None or message.author.id != self.user_id:
                return
            if self._mark_event_key(("delete", message.id)):
                return  # the raw handler already published this dispatch
            guild = getattr(message, "guild", None)
            self.on_message_delete(
                message.id, message.channel.id,
                getattr(guild, "id", None) if guild else None, message,
            )

        @self._client.event
        async def on_reaction_add(reaction: Any, user: Any) -> None:
            if self.on_reaction is None or user.id != self.user_id:
                return
            message = reaction.message
            emoji_text = _emoji_repr(reaction.emoji)
            if self._mark_event_key(
                    ("reaction", "add", message.id, user.id, emoji_text)):
                return  # the raw handler already published this dispatch
            guild = (getattr(message, "guild", None)
                     or getattr(getattr(message, "channel", None), "guild", None))
            self.on_reaction(
                "add", message.id, message.channel.id,
                getattr(guild, "id", None) if guild else None,
                user.id, emoji_text, (reaction, user),
            )

        @self._client.event
        async def on_reaction_remove(reaction: Any, user: Any) -> None:
            if self.on_reaction is None or user.id != self.user_id:
                return
            message = reaction.message
            emoji_text = _emoji_repr(reaction.emoji)
            if self._mark_event_key(
                    ("reaction", "remove", message.id, user.id, emoji_text)):
                return  # the raw handler already published this dispatch
            guild = (getattr(message, "guild", None)
                     or getattr(getattr(message, "channel", None), "guild", None))
            self.on_reaction(
                "remove", message.id, message.channel.id,
                getattr(guild, "id", None) if guild else None,
                user.id, emoji_text, (reaction, user),
            )

        # ------------------------------------------------ raw (uncached) events
        # Round-3 F4-3: the rich handlers above fire ONLY for cached
        # messages (state.py:1848-1937; the cache is bounded at 1000 and
        # EMPTY after a restart) — precisely the old-message kakera
        # reaction events this framework feeds on would be silently
        # missed. The fork dispatches raw_* for EVERY dispatch, raw FIRST,
        # so the raw handlers below are the primary publishers and the
        # rich ones suppress as duplicates (see _mark_event_key).

        @self._client.event
        async def on_raw_message_edit(payload: Any) -> None:
            if self.on_message_edit is None:
                return
            message = getattr(payload, "message", None)
            if message is None:
                return
            author = getattr(message, "author", None)
            if author is not None and getattr(author, "id", None) == self.user_id:
                return
            self.on_message_edit(convert_message(
                message,
                dm=isinstance(getattr(message, "channel", None),
                              self._discord.DMChannel),
            ))
            self._mark_event_key(("edit", payload.message_id))

        @self._client.event
        async def on_raw_message_delete(payload: Any) -> None:
            if self.on_message_delete is None:
                return
            cached = getattr(payload, "cached_message", None)
            # self-authored deletes stay filtered when the cache can tell;
            # for UNCACHED deletes the author is unknowable and the event
            # MUST still flow (that is the entire point of the raw path)
            author = getattr(cached, "author", None)
            if author is not None and getattr(author, "id", None) == self.user_id:
                return
            self.on_message_delete(
                payload.message_id, payload.channel_id,
                getattr(payload, "guild_id", None), cached,
            )
            self._mark_event_key(("delete", payload.message_id))

        @self._client.event
        async def on_raw_reaction_add(payload: Any) -> None:
            self._publish_raw_reaction("add", payload)

        @self._client.event
        async def on_raw_reaction_remove(payload: Any) -> None:
            self._publish_raw_reaction("remove", payload)

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
        self.connected = True
        self._connected_since = time.monotonic()  # `pg` uptime anchor
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
            self.connected = False  # start() returned: the session is over
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

    def _install_command_layer(self, discord: Any) -> None:
        """Register the self-command surface on the ext.commands Bot.

        The Bot runs in user_bot mode: dispatch receives messages from
        EVERYONE, and each invocation is gated on the actual TYPER being
        an OWNER_USER_IDS owner (allowed_invoker — fail-closed on an
        empty owner set or an unknown invoker). Any owner can command
        any framework account; non-owners get no reply at all.

        The concrete verb set comes from the process-wide self-command
        registry (:data:`core.plugin_manager.commands.COMMAND_HUB`): the
        plugin manager publishes an account's (assigned ∧ enabled)
        commands there and this layer rebuilds its Bot registrations from
        whatever is current — framework verbs (help/pg/say) plus every
        plugin-declared command (a one-liner ``COMMANDS`` entry). There
        are no legacy trigger pass-throughs: every verb is a registry
        command.

        Unknown commands (anything after the prefix that isn't registered)
        are silently ignored — they do NOT reach the event bus.
        """
        from discord.ext import commands as ext_commands
        bot = self._client
        adapter = self

        @bot.event
        async def on_command_error(ctx, error):
            # unknown commands are NOT pass-throughs — silently drop them
            # (a typo'd prefix must not leak content into the event bus)
            if not isinstance(error, ext_commands.errors.CommandNotFound):
                adapter._logger.debug(
                    "self-command error: %s", error, exc_info=True)

        # registry-driven verbs: subscribe delivers the CURRENT set and
        # every later publish (assignment/enable mutations) rebuilds us
        COMMAND_HUB.subscribe(
            self._account, self.set_plugin_commands,
            stats_provider=self._self_command_stats,
        )

    # ------------------------------------------------------- self-commands
    def set_plugin_commands(self, specs: Any) -> None:
        """Adopt a newly published command set (hub → client).

        Stores the specs and (once the Bot + command layer exist) rebuilds
        the Bot registrations by DIFF: names that disappeared are removed,
        names that appeared are registered — assignment/enable mutations
        take effect on the live account without a reconnect.
        """
        self._self_command_specs = {
            getattr(spec, "name", ""): spec for spec in (specs or ())
            if getattr(spec, "name", "")
        }
        self._rebuild_self_commands()

    def _rebuild_self_commands(self) -> None:
        """Sync the Bot's registered registry commands with the stored set."""
        bot = self._client
        register = getattr(bot, "command", None)
        if bot is None or not callable(register):
            return  # no Bot (no prefix / pre-connect): specs stay stored
        wanted = set(self._self_command_specs)
        for stale in sorted(self._self_command_names - wanted):
            try:
                bot.remove_command(stale)
            except Exception:
                pass
        for fresh in sorted(wanted - self._self_command_names):
            self._register_self_command(fresh)
        self._self_command_names = wanted

    def _register_self_command(self, name: str) -> None:
        """Register ONE registry command as a thin Bot pass-through."""
        bot = self._client
        adapter = self

        async def _invoke(ctx):
            await adapter._dispatch_self_command(ctx, name)

        try:
            bot.command(name=name)(_invoke)
        except Exception:
            self._logger.exception("self-command registration failed: %s", name)

    def allowed_invoker(self, invoker_id: Optional[int]) -> bool:
        """The ownership gate for one invocation: the TYPER must be an
        OWNER_USER_IDS owner. user_bot mode lets every message reach
        dispatch, so this is the only thing standing between a stranger
        and a command — fail-closed when the owner set is empty or the
        invoker is unknown.
        """
        if not self._owner_ids or invoker_id is None:
            return False
        return invoker_id in self._owner_ids

    def self_command_allowed(self) -> bool:
        """Account-level view of the gate: is the ACCOUNT'S OWN user id
        an owner? Kept for callers that ask whether the account may
        command ITSELF (typing as this account) — actual dispatch gates
        on the real invoker via allowed_invoker(ctx.author.id).
        """
        return self.allowed_invoker(self.user_id)

    def _self_command_stats(self) -> dict:
        """Runtime stats for the framework ``pg`` verb (hub-pulled)."""
        uptime = ((time.monotonic() - self._connected_since)
                  if self._connected_since is not None else None)
        return {"latency_ms": self.latency_ms, "uptime_s": uptime}

    @staticmethod
    def _parse_invocation(content: Any, name: str) -> tuple:
        """``"sm!rollstart 123"`` + ``rollstart`` -> ``(["123"], "123")``.

        Token-based: the first token ENDING with the command name is the
        invocation word (works for every prefix form — plain, @me-mention
        and case variants); everything after it is the argument list
        (whitespace-normalized raw). Library-parsed views are avoided on
        purpose so this helper stays unit-testable without the library.
        """
        tokens = (content or "").split()
        lowered = name.lower()
        for index, token in enumerate(tokens):
            if token.lower().endswith(lowered):
                args = tokens[index + 1:]
                return args, " ".join(args)
        return [], ""

    async def _dispatch_self_command(self, ctx: Any, name: str) -> None:
        """One invocation: owner gate, parse, hub dispatch (contained)."""
        try:
            # user_bot mode delivers every message — gate on the REAL
            # typer (any owner may command this account; the account's
            # own id passes too when the account itself is an owner)
            invoker = getattr(getattr(ctx, "author", None), "id", None)
            if not self.allowed_invoker(invoker):
                self._logger.debug(
                    "self-command %s ignored: invoker %s is not an owner",
                    name, invoker)
                return
            args, raw = self._parse_invocation(
                getattr(ctx, "message", None) and ctx.message.content, name)
            channel = ctx.channel
            guild = getattr(channel, "guild", None)
            guild_id = getattr(guild, "id", None) if guild is not None else None
            await COMMAND_HUB.invoke(
                self._account, name, args, channel.id,
                guild_id=guild_id, invoker_id=invoker, raw=raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            # a broken dispatch must never kill the Bot's command task
            self._logger.exception("self-command dispatch failed: %s", name)

    async def disconnect(self) -> None:
        """Close the underlying client; safe when never started."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as exc:  # closing a half-open client can throw
                self._logger.warning("live client close raised %s", exc)
        self.connected = False
        self._connected_since = None
        if self._command_prefix:
            # drop this client's registry subscription (a reconnect's fresh
            # client re-subscribes with its own rebuild callback)
            try:
                COMMAND_HUB.unsubscribe(self._account)
            except Exception:
                pass
        await self._cancel_run_task()

    # ------------------------------------------------------------- messaging
    def _require_client(self) -> Any:
        """The live client, or ConnectionError when not connected."""
        if self._client is None or not self.connected:
            raise ConnectionError("live client is not connected")
        return self._client

    def _map_send_error(self, exc: Exception, channel_id: int) -> Optional[Exception]:
        """Translate one library send error into a domain error (or None).

        * RateLimited     -> ServerRateLimit (fork errors.RateLimited is NOT
          an HTTPException; carries retry_after from the exceeded
          max_ratelimit_timeout path)
        * CaptchaRequired -> AccessDenied (4xx captcha_key wall — an
          auth-shaped refusal, never retried)
        * HTTPException 429 -> ServerRateLimit (Retry-After float-parsable
          header when present, else 1.0; global flag from X-RateLimit-Global)
        * Forbidden        -> AccessDenied(account, reason)
        * NotFound         -> TargetGone("", reason)
        * 5xx/DiscordServerError -> ConnectionError (transient: the library
          already retried internally before surfacing DiscordServerError)
        """
        name = type(exc).__name__
        if name == "RateLimited":
            try:
                retry_after = max(0.0, float(getattr(exc, "retry_after", 1.0)))
            except (TypeError, ValueError):
                retry_after = 1.0
            if not math.isfinite(retry_after):
                retry_after = 1.0
            return ServerRateLimit(retry_after, False)
        if name == "CaptchaRequired":
            return AccessDenied(
                self._account or "",
                f"discord captcha challenge on channel {channel_id}: {exc}",
            )
        if name == "DiscordServerError" or (
            getattr(exc, "status", None) is not None
            and getattr(exc, "status", 0) >= 500
        ):
            return ConnectionError(f"discord server error ({exc})")
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
            if not math.isfinite(retry_after):
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

    def _harvest_bucket_state(self) -> None:
        """Read the library's per-route bucket state after a send.

        discord.py-self's HTTPClient maintains `Ratelimit` objects keyed by
        `{bucket_hash}:{major_parameters}` — updated from X-RateLimit-*
        headers on every response. For write routes (message sends,
        reactions) Discord DOES send these headers; the library tracks
        limit/remaining/reset_after per route+channel.

        We store the LATEST state per action for the engine to read via
        `get_bucket_state()`. This is the data that lets the engine pace
        by Discord's ACTUAL budget instead of fixed intervals.
        """
        http = getattr(self._client, "http", None)
        if http is None:
            return
        buckets = getattr(http, "_buckets", None)
        if not buckets:
            return
        now = getattr(self._client, "loop", None)
        now = now.time() if now else __import__("time").monotonic()
        harvested: Dict[str, dict] = {}
        for key, rl in buckets.items():
            if not hasattr(rl, "remaining"):
                continue
            harvested[key] = {
                "limit": getattr(rl, "limit", 0),
                "remaining": getattr(rl, "remaining", 0),
                "reset_after_s": getattr(rl, "reset_after", 0.0),
                "expires": getattr(rl, "expires", None),
                "outgoing": getattr(rl, "outgoing", 0),
            }
        if harvested:
            self._bucket_state = harvested
            self._bucket_state_at = now

    def get_bucket_state(self) -> Dict[str, dict]:
        """The latest per-route bucket state from the library (may be {})."""
        return getattr(self, "_bucket_state", {})

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
        self._harvest_bucket_state()  # feed Discord's limits to the engine
        # BUCKET-AWARE PACING: harvest Discord's per-route limits after the
        # send so the engine can read them before the NEXT acquire().
        # Only write routes carry headers; GET-only routes harvest nothing
        # and the engine falls back to policy pacing.
        harvest = getattr(self, "_harvest_bucket_state", None)
        if callable(harvest):
            harvest()
        return DiscordMessage(
            id=getattr(sent, "id", 0),
            channel_id=channel_id,
            author_id=self.user_id or 0,
            content=content,
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

    async def _reap_click_task(self, task: "asyncio.Task") -> None:
        """Await a cancelled click task, swallowing its death exceptions —
        unless OUR caller's cancellation is pending (never eat that)."""
        try:
            await task
        except asyncio.CancelledError:
            if outer_cancel_pending():
                raise
        except Exception:
            pass

    async def _click_component(self, component: Any, channel_id: int) -> Any:
        """Click with a hard total-await ceiling (round-3 F4-5/F4-6).

        * the fork's 202-with-code-110000-119999 interaction path retries
          by sleeping ``retry_after`` UNCAPPED and exempt from
          max_ratelimit_timeout (http.py:936-948) — asyncio.wait_for
          bounds one click to CLICK_TIMEOUT_S and a TimeoutError maps to
          the domain's transient vocabulary (ConnectionError, same as the
          5xx mapping) so the scheduler retries rather than hangs;
        * _wrapped_interaction converts CancelledError into InvalidData
          (interactions.py:215-216), so a stop()-driven cancel of a click
          in flight would masquerade as a data error — when the enclosing
          task's cancellation is pending, re-raise CancelledError.
        """
        task = asyncio.get_running_loop().create_task(component.click())
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), CLICK_TIMEOUT_S)
        except TimeoutError:
            task.cancel()
            await self._reap_click_task(task)
            raise ConnectionError(
                f"button click on channel {channel_id} did not settle "
                f"within {CLICK_TIMEOUT_S:.0f}s (interaction retry loop "
                "or stalled gateway)"
            ) from None
        except Exception as exc:
            if (type(exc).__name__ == "InvalidData"
                    and outer_cancel_pending()):
                task.cancel()
                await self._reap_click_task(task)
                raise asyncio.CancelledError from exc
            mapped = self._map_send_error(exc, channel_id)
            if mapped is not None:
                raise mapped from exc
            raise
        finally:
            if not task.done():
                task.cancel()
                await self._reap_click_task(task)

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
                    return await self._click_component(component, channel_id)
        from core.errors import TargetGone

        raise TargetGone("", f"custom_id {custom_id!r} not on message {message_id}")

    async def fetch_user(self, user_id: int) -> Any:
        """Fetch a user object; None when the user is gone (NotFound).

        The services seam (resolve_user) documents None-on-unresolvable —
        letting NotFound propagate violated that contract live. Discord
        ALSO answers 403 "Unknown User" (code 10013) for missing users on
        the user endpoint, so that Forbidden is a gone-signal too.
        """
        try:
            return await self._require_client().fetch_user(user_id)
        except Exception as exc:
            if type(exc).__name__ == "NotFound":
                return None
            if (type(exc).__name__ == "Forbidden"
                    and "unknown user" in str(exc).lower()):
                return None
            raise

    # ---------------------------------------------------------- observability
    @property
    def latency_ms(self) -> Optional[float]:
        """Gateway heartbeat latency in ms (None before READY / when NaN/inf).

        Note (round-3 F4-9): once connected the library's ``latency`` is
        always a float — the None paths here are the pre-connect and
        non-finite sentinel cases, so callers see float-or-None, never a
        NaN/inf leak.
        """
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
        if math.isnan(latency) or math.isinf(latency) or latency < 0:
            return None
        return latency * 1000.0

    def cached_snapshot(self) -> dict:
        """In-memory view only — NO network fetches (details page source).

        Uses exactly what the live client already holds in cache after READY:
        our user, guilds + their channels, open DM channels (recipient ids),
        and the heartbeat latency. ``{"available": False}`` before connect.
        ``dm_channels`` entries follow the flat ``{"id", "recipient"}``
        contract (round-3 F4-9 parity nit — recipient is the OTHER user's
        id, never an embedded object).
        """
        client = self._client
        if client is None or self.user_id is None or not self.connected:
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

    def channels_snapshot(self) -> list:
        """Picker-shaped text-channel snapshot — memory only, NO fetches.

        ``[{"guild_id": str, "guild_name": str, "channels": [{"id": str,
        "name": str, "type": str}]}]`` — TEXT channels only (text/news;
        categories, voice, stage and forum rows are skipped), sorted by
        guild name then channel name, ids as STRINGS. [] before connect.
        Source of :meth:`ControlAPI.accounts_channels` (the Phase-2
        channel picker); read-only over exactly what the live client
        holds after READY, same discipline as cached_snapshot.
        """
        client = self._client
        if client is None or not self.connected:
            return []
        out: list = []
        for guild in getattr(client, "guilds", []) or []:
            channels: list = []
            for channel in getattr(guild, "channels", []) or []:
                ctype = getattr(channel, "type", None)
                type_name = (getattr(ctype, "name", None)
                             or str(ctype)).lower()
                if type_name not in _TEXT_CHANNEL_TYPES:
                    continue
                channels.append({
                    "id": str(getattr(channel, "id", "")),
                    "name": getattr(channel, "name", None) or "",
                    "type": type_name,
                })
            channels.sort(key=lambda c: c["name"])
            out.append({
                "guild_id": str(getattr(guild, "id", "")),
                "guild_name": getattr(guild, "name", None) or "",
                "channels": channels,
            })
        out.sort(key=lambda g: g["guild_name"])
        return out


class IdentifyGate:
    """Fleet-wide IDENTIFY pacer (Discord's identify limit is per-IP).

    discord.py-self 2.1.0 ships NO identify serializer — the library's
    ``before_identify_hook`` is a no-op by default — so N clients in one
    process can IDENTIFY simultaneously and trip Discord's per-IP gateway
    connection limit (observed live as ``Connection reset by peer`` during
    startup and, worse, during synchronized reconnect storms that
    ``start_all``'s stagger does not cover). One gate instance is shared
    by every client the factory builds; waiters are released FIFO with
    at least ``effective_interval`` seconds between consecutive IDENTIFYs
    (isolation audit K8 / S3-3.1, S7-A2).

    Fleet-size scaling (round-3 F4-2): the library awaits IDENTIFY — and
    therefore this hook — under ``asyncio.wait_for(..., 60)`` (client.py:
    1020; the hook runs inside DiscordWebSocket.from_client), so a waiter
    queued >= 60s is cancelled into a disconnect dispatch → reconnect
    livelock once a synchronized (re)identify wave exceeds ~60s of
    queueing (>= ~13 accounts at a flat 5s interval). The gate instead
    derives its spacing from the EXPECTED fleet size::

        effective_interval = min(interval,
                                 max(min_interval, wait_budget / expected))

    with ``wait_budget`` 50s (margin under the 60s library ceiling), so
    the LAST waiter of an N-account wave still clears the hook in <= ~50s.
    ``expected`` is a settable count (the factory raises it as it builds
    DISTINCT accounts — reconnects rebuild already-counted accounts and
    never inflate it; the account manager may also set it directly when
    the fleet size is known ahead of boot). It is re-read on every
    release, so a fleet that grows mid-wave tightens spacing dynamically.
    """

    #: total seconds a synchronized wave may spend inside the hook (< 60)
    WAIT_BUDGET_S = 50.0
    #: never pace IDENTIFYs tighter than this (Discord per-IP friendliness)
    MIN_INTERVAL_S = 1.0

    __slots__ = ("interval", "expected", "min_interval", "wait_budget",
                 "_lock", "_last", "_depth")

    def __init__(self, interval: float = 5.0, *, min_interval: float = MIN_INTERVAL_S,
                 wait_budget: float = WAIT_BUDGET_S,
                 max_wait: Optional[float] = None) -> None:
        self.interval = float(interval)
        if max_wait is not None:  # upstream phase-4 alias (tests/config)
            wait_budget = max_wait
        #: expected fleet size (settable; see class docstring)
        self.expected = 1
        self.min_interval = float(min_interval)
        self.wait_budget = float(wait_budget)
        self._lock: Optional[asyncio.Lock] = None  # bound lazily to the loop
        self._last: float = float("-inf")
        self._depth: int = 0  # FINAL-PHASE (F4): FIFO waiters ahead-of-lock

    async def acquire(self, shard_id=None, *, initial: bool = False) -> None:
        """Space consecutive IDENTIFYs by ``effective_interval`` seconds.

        The leading ``shard_id`` positional is tolerated (never used —
        user accounts are unsharded — but discord.py lineage hooks are
        invoked with it positionally, and library drift must not turn
        the gate into a TypeError). Fast-fails with a retryable
        ConnectionError when the PROJECTED total wait (residual spacing
        + waiters already queued ahead × interval) exceeds the wave
        budget (FINAL-PHASE F4 + round-3: the effective interval scales
        with the expected fleet size so a synchronized wave of ANY size
        clears under the library's 60s identify ceiling — the fast-fail
        now covers only genuinely pathological saturation, e.g. a
        min_interval floor met with a huge fleet).
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        interval = self.effective_interval
        # projected wait BEFORE queueing: residual + the queue depth ahead
        # of us each costs a full interval (FIFO under the held lock)
        loop = asyncio.get_running_loop()
        projected = (self._last + interval * (self._depth + 1)
                     - loop.time()) if self._last != float("-inf") else 0.0
        if projected > self.wait_budget:
            raise ConnectionError(
                f"identify gate saturated: projected wait {projected:.0f}s "
                f"exceeds {self.wait_budget:.0f}s ({self._depth} queued) — "
                "retrying with backoff is cheaper than parking a built client")
        self._depth += 1
        try:
            async with self._lock:  # FIFO: hold while sleeping so waiters queue
                now = loop.time()
                wait = self._last + self.effective_interval - now
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = loop.time()
                self._last = now
        finally:
            self._depth -= 1

    @property
    def effective_interval(self) -> float:
        """Current spacing, derived from the expected fleet size."""
        expected = max(1, int(self.expected))
        return min(self.interval,
                   max(self.min_interval, self.wait_budget / expected))


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
        command_prefix: str = "",
    ) -> None:
        self._credentials = credentials  # kept for metadata only (mudae id, owner ids)
        self._logger = logger or LOG
        # ONE gate per process (the factory is built once in bootstrap):
        # every client this factory produces IDENTIFYs through it.
        self._identify_gate = IdentifyGate(identify_interval)
        #: self-command prefix shared by every client (from SELF_COMMAND_PREFIX)
        self._command_prefix = command_prefix or ""
        #: distinct accounts this factory has EVER built a client for —
        #: reconnects rebuild already-counted accounts, so the gate's
        #: ``expected`` never ratchets down through reconnect cycles
        self._gate_accounts: set = set()

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
        if name:
            # round-3 F4-2: the gate paces a synchronized fleet wave inside
            # the library's 60s identify ceiling (client.py:1020)
            self._gate_accounts.add(name)
            self._identify_gate.expected = len(self._gate_accounts)
        # K13 (S9-C1): a per-account child logger keeps adapter lines
        # (connected/terminated/close-failed) attributable — the bridge
        # maps slaughtermudae.adapter.<name> to source "adapter:<name>".
        logger = self._logger.getChild(name) if name else self._logger
        client = RealDiscordClient(
            token=token, logger=logger, identify_gate=self._identify_gate,
            command_prefix=self._command_prefix,
            owner_ids=frozenset(
                getattr(self._credentials, "owner_user_ids", ()) or ()),
        )
        client.account = name  # enrich AccessDenied reasons, best effort
        return client


__all__ = ["IdentifyGate", "RealDiscordClient", "RealClientFactory"]
