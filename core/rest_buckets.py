"""REST bucket manager driven by Discord's own rate-limit headers.

User tokens are special (researched, cross-checked against Discord's docs and
the discord.py-self docs): user authorization usually only returns
`Retry-After`, `X-RateLimit-Global` and `X-RateLimit-Scope` — the full
`X-RateLimit-Limit/Remaining/Reset/Bucket` set is a bot/OAuth convenience.
discord.py-self therefore maintains its own per-route tables and prevents
429s proactively.

Design consequences for this framework:
  1. consume headers WHEN PRESENT (bucket-id keyed, honoring Reset-After);
  2. always honor 429 Retry-After (+ Global flag via a fleet-wide callback);
  3. when neither is available, fall back to a conservative per-route-key
     minimum spacing so we never fire blind into an unknown bucket.

Buckets are keyed by `X-RateLimit-Bucket` when present, otherwise by the
route key (which already includes the top-level resource — guild/channel id —
that is how Discord scopes per-route/per-guild limits).

`RestGate` is the single-flight gate: one REST call at a time per account,
others wait (user spec).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

LOG = logging.getLogger("slaughtermudae.rest")

#: conservative fallback spacing (seconds) for route keys we know nothing
#: about — used only while no headers have been observed for the key
DEFAULT_ROUTE_SPACING: Dict[str, float] = {
    "message": 0.35,
    "dm": 0.35,
    "fetch": 0.6,
    "react": 0.5,
}

#: hard cap on trusting a Reset-After (protects against absurd header values)
MAX_TRUSTED_RESET_AFTER = 600.0


class RestCooldown(RuntimeError):
    """Discord's own rate limit said WAIT (429 Retry-After / exhausted bucket)."""

    def __init__(self, route_key: str, retry_after: float, global_: bool = False) -> None:
        scope = "GLOBAL" if global_ else route_key
        super().__init__(f"REST cooldown {scope} for {retry_after:.2f}s")
        self.route_key = route_key
        self.retry_after = retry_after
        self.global_ = global_


@dataclass
class _Bucket:
    bucket_id: Optional[str]
    remaining: Optional[float] = None
    reset_at: float = 0.0
    observed_limit: Optional[int] = None
    scope: str = ""
    fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RestFeedback:
    """What an adapter reports after one REST call (headers may be sparse)."""

    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    retry_after_body: Optional[float] = None


class RestBucketManager:
    """Per-account manager of Discord's REST rate-limit state."""

    def __init__(
        self,
        account: str,
        on_global_cooldown: Optional[Callable[[float], None]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.account = account
        self._on_global = on_global_cooldown
        self._logger = logger or LOG
        self._buckets: Dict[str, _Bucket] = {}
        self._route_to_bucket: Dict[str, str] = {}
        self._last_call_at: Dict[str, float] = {}
        self._route_cooldown_until: Dict[str, float] = {}  # 429 cooldowns (no bucket needed)
        self._global_until = 0.0
        self._headers_seen = 0
        self._route_fallback = dict(DEFAULT_ROUTE_SPACING)

    # ---------------------------------------------------------------- intake
    def after_call(self, route_key: str, feedback: RestFeedback) -> Dict[str, Any]:
        """Feed Discord's response state back after one REST call."""
        now = time.monotonic()
        self._last_call_at[route_key] = now
        decision: Dict[str, Any] = {"route": route_key, "status": feedback.status}

        lowered = {
            k.lower().replace("_", "-"): v
            for k, v in (feedback.headers or {}).items()
            if v is not None
        }
        bucket_id = lowered.get("x-ratelimit-bucket")
        retry_after = feedback.retry_after_body
        if retry_after is None and "retry-after" in lowered:
            try:
                retry_after = float(lowered["retry-after"])
            except ValueError:
                retry_after = None

        if feedback.status == 429:
            wait = retry_after or float(lowered.get("x-ratelimit-reset-after", 1.0) or 1.0)
            wait = min(max(wait, 0.5), MAX_TRUSTED_RESET_AFTER)
            global_hit = lowered.get("x-ratelimit-global") == "true"
            decision.update({"cooldown": round(wait, 2), "global": global_hit, "source": "429"})
            self._cooldown(route_key, wait, global_hit)
            return decision

        if bucket_id is None:
            # user-token path: sparse headers — conservative spacing only
            if any(k.startswith("x-ratelimit") for k in lowered):
                self._headers_seen += 1
            decision["mode"] = "sparse"
            return decision

        self._headers_seen += 1
        self._route_to_bucket[route_key] = bucket_id
        bucket = self._buckets.setdefault(bucket_id, _Bucket(bucket_id=bucket_id))
        try:
            remaining = float(lowered["x-ratelimit-remaining"])
            reset_after = float(lowered.get("x-ratelimit-reset-after", "0") or 0.0)
        except (KeyError, ValueError):
            decision["mode"] = "partial-headers"
            return decision
        reset_after = min(reset_after, MAX_TRUSTED_RESET_AFTER)
        bucket.remaining = remaining
        bucket.reset_at = now + max(0.0, reset_after)
        bucket.scope = lowered.get("x-ratelimit-scope", bucket.scope)
        if "x-ratelimit-limit" in lowered:
            try:
                bucket.observed_limit = int(lowered["x-ratelimit-limit"])
            except ValueError:
                pass
        decision.update(mode="headers", bucket=bucket_id[:8], remaining=remaining,
                        reset_after=round(reset_after, 2))
        return decision

    # ---------------------------------------------------------------- gates
    def wait_before(self, route_key: str) -> float:
        """Seconds to WAIT before this call (0.0 = go now).

        Considers: global 429 cooldown, bucket exhaustion (when headers were
        seen), and conservative spacing for unknown route keys.
        """
        now = time.monotonic()
        waits = [0.0]
        if now < self._global_until:
            waits.append(self._global_until - now)
        route_cooldown = self._route_cooldown_until.get(route_key, 0.0)
        if now < route_cooldown:
            waits.append(route_cooldown - now)
        bucket_id = self._route_to_bucket.get(route_key)
        if bucket_id:
            bucket = self._buckets.get(bucket_id)
            if bucket is not None:
                if bucket.remaining is not None and bucket.remaining <= 0 and now < bucket.reset_at:
                    waits.append(bucket.reset_at - now)
                if now >= bucket.reset_at and bucket.remaining is not None and bucket.remaining <= 0:
                    bucket.remaining = float(bucket.observed_limit or 1)  # refilled
        spacing = self._route_fallback.get(route_key, 0.0)
        last = self._last_call_at.get(route_key)
        if last is not None and spacing > 0:
            waits.append(max(0.0, spacing - (now - last)))
        return max(waits)

    def _cooldown(self, route_key: str, wait: float, global_hit: bool) -> None:
        now = time.monotonic()
        if global_hit:
            self._global_until = now + wait
            if self._on_global is not None:
                try:
                    self._on_global(wait)
                except Exception:
                    self._logger.exception("global cooldown callback failed")
            self._logger.warning("GLOBAL 429 for %s: pausing account %.1fs", route_key, wait)
            return
        bucket_id = self._route_to_bucket.get(route_key)
        if bucket_id and bucket_id in self._buckets:
            self._buckets[bucket_id].remaining = 0.0
            self._buckets[bucket_id].reset_at = now + wait
        # ERROR AUDIT (rest): cooldown applies even without a known bucket —
        # user tokens rarely expose bucket ids, but Discord still said WAIT
        self._route_cooldown_until[route_key] = now + wait
        self._logger.warning("429 on %s: cooling %.1fs", route_key, wait)

    # ------------------------------------------------------------- tunables
    def set_route_spacing(self, route_key: str, seconds: float) -> None:
        self._route_fallback[route_key] = max(0.0, float(seconds))

    def snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        buckets = [
            {
                "bucket": bid[:8],
                "remaining": b.remaining,
                "resets_in": round(max(0.0, b.reset_at - now), 2),
                "limit": b.observed_limit,
                "scope": b.scope,
            }
            for bid, b in sorted(self._buckets.items())
        ]
        routes = {}
        for key in sorted(set(self._route_to_bucket) | set(self._last_call_at) | set(self._route_fallback) | set(self._route_cooldown_until)):
            ts = self._last_call_at.get(key)
            cooldown = self._route_cooldown_until.get(key, 0.0)
            routes[key] = {
                "bucket": (self._route_to_bucket.get(key) or "")[:8] or None,
                "last_call_ago": round(now - ts, 2) if ts else None,
                "min_spacing": self._route_fallback.get(key, 0.0),
                "cooldown_s": round(max(0.0, cooldown - now), 2),
            }
        return {
            "account": self.account,
            "headers_seen": self._headers_seen,
            "global_cooldown_s": round(max(0.0, self._global_until - now), 2),
            "buckets": buckets,
            "routes": routes,
        }


class RestGate:
    """Single-flight REST gate: one call at a time per account, others wait.

    Usage (adapter/session):
        async with gate.call("fetch", action_coro_factory) as run:
            result = await run()
        # adapter then reports headers: gate.report("fetch", RestFeedback(...))
    """

    def __init__(self, manager: RestBucketManager) -> None:
        self.manager = manager
        self._lock: Optional[asyncio.Lock] = None

    def _lock_or_create(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def report(self, route_key: str, feedback: RestFeedback) -> Dict[str, Any]:
        """Feed response state back (status/headers/retry-after) to the manager."""
        return self.manager.after_call(route_key, feedback)

    def call(
        self,
        route_key: str,
        action: Callable[[], Awaitable[Any]],
    ) -> "_RestCallContext":
        """Async context manager: serialize + pre-space, then run the action."""
        return _RestCallContext(self, route_key, action)


class _RestCallContext:
    def __init__(self, gate: RestGate, route_key: str, action: Callable[[], Awaitable[Any]]) -> None:
        self._gate = gate
        self._route_key = route_key
        self._action = action
        self._lock: Optional[asyncio.Lock] = None

    async def __aenter__(self) -> Callable[[], Awaitable[Any]]:
        self._lock = self._gate._lock_or_create()
        await self._lock.acquire()
        wait = self._gate.manager.wait_before(self._route_key)
        if wait > 0:
            await asyncio.sleep(wait)
        return self._action  # caller: result = await action()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._lock.release()
        return False  # never swallow action exceptions

    def feedback(self, feedback: RestFeedback) -> Dict[str, Any]:
        """Report this call's response state (call after the action returns)."""
        return self._gate.report(self._route_key, feedback)


class RestCooldownError(RestCooldown):
    """Raised form of RestCooldown for callers that prefer exceptions."""


__all__ = [
    "RestBucketManager", "RestFeedback", "RestGate", "RestCooldown",
    "RestCooldownError", "DEFAULT_ROUTE_SPACING", "MAX_TRUSTED_RESET_AFTER",
]
