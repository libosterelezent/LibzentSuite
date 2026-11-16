"""Rate-limit manager package (A4): 4-level policies, pacing engine,
server-limit cooldowns, and safety warnings.

Public surface (pinned by INTEGRATION.md):

    RateLimitManager   policies + acquire() pacing + server limits + stats
                        + the server-wide sequential send lane (GlobalSendLane)
    RatePolicy         frozen policy value object (interval + jitter window)
    Grant              acquire() receipt (wait_s / jitter_s / reason)
    MIN_ACTION_INTERVAL  hard safety floor constant (1.5s per action)
    GlobalSendLane     FarmLite-style sequential gate, robust port (lane.py)

Design decisions (documented per the INTEGRATION ground rules):
* interval+pacing instead of the audited token buckets; priority waiter
  heap, wake events, NaN validation and jitter seeding are ported ideas.
* effective() composes policy levels by MAX interval (never faster than ANY
  applicable level) and never paces faster than the safety floor. With NO
  stored policy the LEARNED per-action default applies — and any stored
  policy REPLACES (displaces) that default rather than composing max()
  with it, so prefixrate overrides can deliberately pace below the study
  rate; the floor still applies on every path.
* persistence is best-effort: pacing safety never depends on the database.
"""

from .engine import (
    KV_FLOOR_KEY,
    KV_LANE_KEY,
    KV_REMOVED_KEY,
    UNSTUDIED_ACTION_INTERVAL,
    Clock,
    Grant,
    ManualClock,
    RateLimitManager,
    SystemClock,
)
from .lane import MAX_PREFIXRATE, GlobalSendLane
from .policies import MIN_ACTION_INTERVAL, RatePolicy

__all__ = [
    "KV_FLOOR_KEY",
    "KV_LANE_KEY",
    "KV_REMOVED_KEY",
    "MAX_PREFIXRATE",
    "MIN_ACTION_INTERVAL",
    "UNSTUDIED_ACTION_INTERVAL",
    "Clock",
    "GlobalSendLane",
    "Grant",
    "ManualClock",
    "RateLimitManager",
    "RatePolicy",
    "SystemClock",
]
