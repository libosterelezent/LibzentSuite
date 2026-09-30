"""Domain errors for flow control and cache invalidation.

`FlowBlocked` is the contract plugins rely on: when a selfbot's flow hits a
dead end (channel deleted, access revoked, cache invalid), raising a
FlowBlocked subclass stops THAT account's flow with a human-readable reason
while every other account keeps running (per-account isolation is the
container's job).
"""
from __future__ import annotations


class FlowBlocked(RuntimeError):
    """A selfbot flow cannot continue, with a reason.

    Raising this aborts the current plugin flow for ONE account. Other
    accounts/plugins are unaffected (the container's dispatcher contains it).
    """

    def __init__(self, account: str, reason: str) -> None:
        super().__init__(f"[{account}] flow blocked: {reason}")
        self.account = account
        self.reason = reason


class CacheInvalid(FlowBlocked):
    """The cached state needed for this action is gone or stale."""


class TargetGone(CacheInvalid):
    """The cached channel/target no longer exists (deleted, cache evicted)."""


class AccessDenied(FlowBlocked):
    """Discord refused access (permissions revoked, banned from channel)."""


class FetchRequired(FlowBlocked):
    """The target is not in cache and fetching is disabled/unavailable."""


class RestCooldown(RuntimeError):
    """Discord's own rate limit said WAIT (429 Retry-After / exhausted bucket)."""

    def __init__(self, route_key: str, retry_after: float, global_: bool = False) -> None:
        scope = "GLOBAL" if global_ else route_key
        super().__init__(f"REST cooldown {scope} for {retry_after:.2f}s")
        self.route_key = route_key
        self.retry_after = retry_after
        self.global_ = global_
