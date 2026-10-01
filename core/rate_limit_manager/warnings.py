"""Safety warnings for the rate-limit configuration (A4).

Taxonomy ported from the audited ``core/warnings.py`` and adapted from token
buckets (n/seconds + delay ranges) to interval+pacing policies:

    aggressive   (warn) interval < 3.0s/action — legal (>= the 1.5s floor)
                 but fast for a selfbot; recommend >= 3s with jitter
    no_jitter    (info) jitter_max == 0 — perfectly metronomic timing looks
                 robotic; add a range (e.g. 0.20..0.80s)
    narrow_jitter(info) a jitter range exists but its spread is < 0.3s —
                 still metronome-like; widen it
    floor        (info) always present; shows the current hard safety floor

Warnings never block anything — the hard floor (UnsafePolicyError at set
time) does the blocking. Warnings are advisory, for the CLI ``ratelimit
warnings`` command and the dashboard banner.

Output shape is pinned by INTEGRATION.md: ``{"level", "code", "message"}``
with level one of ``"danger" | "warn" | "info"``, sorted danger > warn > info.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from .policies import RatePolicy

#: below this interval (even if above the floor) we warn about aggressiveness
AGGRESSIVE_INTERVAL = 3.0

#: jitter ranges narrower than this spread look robotic
NARROW_JITTER_SPREAD = 0.3

#: output ordering: most severe first
_LEVEL_RANK: Dict[str, int] = {"danger": 0, "warn": 1, "info": 2}


def describe_scope(level: str, owner: Optional[str]) -> str:
    """Human description of a policy scope, e.g. ``"global"`` or ``"alpha/soulcopy"``."""
    if level == "global" or not owner:
        return "global"
    return owner if level == "account_plugin" else f"{level} {owner}"


def evaluate_policy(
    policy: RatePolicy, level: str, owner: Optional[str], floor: float
) -> List[dict]:
    """Advisory verdicts for ONE stored policy (never blocks anything).

    Returns zero or more ``{"level", "code", "message"}`` dicts; intervals at
    or above the floor can only produce advisory entries (below-floor values
    cannot be SET, and would surface as "aggressive" here if hand-edited into
    the database).
    """
    out: List[dict] = []
    where = describe_scope(level, owner)
    if policy.interval < AGGRESSIVE_INTERVAL:
        out.append({
            "level": "warn",
            "code": "aggressive",
            "message": (
                f"policy {policy.action!r} at {where} paces every "
                f"{policy.interval:.2f}s — aggressive; prefer >= "
                f"{AGGRESSIVE_INTERVAL:.0f}s/action with jitter (floor is "
                f"{floor:.2f}s)"
            ),
        })
    if policy.jitter_max <= 0:
        out.append({
            "level": "info",
            "code": "no_jitter",
            "message": (
                f"policy {policy.action!r} at {where} has no jitter — add a "
                f"range (e.g. 0.20..0.80s) to avoid metronome-perfect timing"
            ),
        })
    elif policy.jitter_max - policy.jitter_min < NARROW_JITTER_SPREAD:
        out.append({
            "level": "info",
            "code": "narrow_jitter",
            "message": (
                f"policy {policy.action!r} at {where} jitter range "
                f"{policy.jitter_min:.2f}..{policy.jitter_max:.2f}s is narrow "
                f"(< {NARROW_JITTER_SPREAD:.1f}s spread) — widen it for less "
                f"robotic spacing"
            ),
        })
    return out


def floor_warning(floor: float) -> dict:
    """The always-present info entry showing the current hard safety floor."""
    return {
        "level": "info",
        "code": "floor",
        "message": (
            f"safety floor is {floor:.2f}s per action — policies faster than "
            f"this are refused (UnsafePolicyError)"
        ),
    }


def sort_warnings(items: Iterable[dict]) -> List[dict]:
    """Order danger first, then warn, then info (stable + deterministic)."""
    return sorted(
        items,
        key=lambda w: (
            _LEVEL_RANK.get(str(w.get("level")), 99),
            str(w.get("code", "")),
            str(w.get("message", "")),
        ),
    )


def evaluate_policies(
    policies: Iterable[Tuple[RatePolicy, str, Optional[str]]], floor: float
) -> List[dict]:
    """Review every stored policy + the floor entry; sorted danger > warn > info.

    Args:
        policies: iterable of ``(policy, level, owner)`` tuples — every STORED
            policy is reviewed individually (a global default and an account
            override each get their own verdicts, ported audit fix).
        floor: the manager's current safety floor (for message context).
    """
    items: List[dict] = [floor_warning(floor)]
    for policy, level, owner in policies:
        items.extend(evaluate_policy(policy, level, owner, floor))
    return sort_warnings(items)


__all__ = [
    "AGGRESSIVE_INTERVAL",
    "NARROW_JITTER_SPREAD",
    "describe_scope",
    "evaluate_policy",
    "evaluate_policies",
    "floor_warning",
    "sort_warnings",
]
