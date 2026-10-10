"""Safety warnings for the rate-limit configuration (A4).

Taxonomy (recalibrated for Discord-driven pacing):

    below_floor  (danger) interval < the current safety floor — cannot be
                 SET through the API (UnsafePolicyError refuses it), so it
                 means a hand-edited database or a floor raised afterwards
    no_jitter    (info) jitter_max == 0 — perfectly metronomic timing looks
                 robotic; add a range (e.g. 0.20..0.80s)
    narrow_jitter(info) a jitter range exists but its spread is < 0.3s —
                 still metronome-like; widen it

The historical "aggressive < 3s" lint and the always-present floor info
entry were REMOVED: pacing is Discord-driven (bucket-aware blocking is
the primary constraint and the floor enforces the minimum), so a
policy between the floor and 3s is a legitimate configuration, and the
floor value is surfaced by the dashboard's Safety Floor card and the
CLI status — not as a permanent "warning".

Warnings never block anything — the hard floor (UnsafePolicyError at set
time) does the blocking. Warnings are advisory, for the CLI ``ratelimit
warnings`` command and the dashboard banner.

Output shape is pinned by INTEGRATION.md: ``{"level", "code", "message"}``
with level one of ``"danger" | "warn" | "info"``, sorted danger > warn > info.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from .policies import RatePolicy

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

    Returns zero or more ``{"level", "code", "message"}`` dicts. Only a
    below-floor interval rates a warning (danger) — it cannot be SET
    through the API, so seeing one means a hand-edited database or a
    floor raised after the policy was stored. Everything at or above the
    floor is a legitimate Discord-driven pacing configuration.
    """
    out: List[dict] = []
    where = describe_scope(level, owner)
    if policy.interval < floor:
        out.append({
            "level": "danger",
            "code": "below_floor",
            "message": (
                f"policy {policy.action!r} at {where} paces every "
                f"{policy.interval:.2f}s — BELOW the {floor:.2f}s safety "
                f"floor (hand-edited, or the floor was raised after it was "
                f"set); raise the policy"
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
    """The floor as a displayable entry (info).

    NOT part of evaluate_policies() output anymore — a permanent entry is
    residual noise, and the floor is already documented by the dashboard's
    Safety Floor card and the CLI. Kept for callers that want to render it
    deliberately.
    """
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
    """Review every stored policy; sorted danger > warn > info.

    Args:
        policies: iterable of ``(policy, level, owner)`` tuples — every STORED
            policy is reviewed individually (a global default and an account
            override each get their own verdicts, ported audit fix).
        floor: the manager's current safety floor (for message context).

    A clean configuration yields an EMPTY list — no permanent/residual
    entries are prepended.
    """
    items: List[dict] = []
    for policy, level, owner in policies:
        items.extend(evaluate_policy(policy, level, owner, floor))
    return sort_warnings(items)


__all__ = [
    "NARROW_JITTER_SPREAD",
    "describe_scope",
    "evaluate_policy",
    "evaluate_policies",
    "floor_warning",
    "sort_warnings",
]
