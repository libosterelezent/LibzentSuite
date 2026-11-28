"""Rate policies: the 4-level policy model + validation (spec §5, A4).

A policy is a *pacing* rule, not a token bucket (the audited old limiter used
n/window token buckets; this rewrite paces actions with a fixed minimum
interval — same safety goal, simpler mental model):

    interval      minimum seconds between two consecutive fires of the same
                  action on the same pacing key
    jitter_min..jitter_max
                  extra uniformly sampled human-like delay applied AFTER each
                  grant so traffic does not look metronome-perfect

Policy levels (composition is max-interval: an account can never make itself
FASTER than a global rule, only slower — see engine.effective):

    global          owner=None                    every account, every plugin
    account         owner="<account name>"        one account, any plugin
    plugin          owner="<plugin name>"         one plugin, any account
    account_plugin  owner="<account>/<plugin>"    one account+plugin pair

Validation philosophy (ported from the audited code): every value is checked
at construction time — finite numbers (NaN previously slipped through float
comparisons and poisoned buckets), sane caps so a stray 1e9 can not wedge the
fleet, and precise human-readable RatePolicyError messages.

The *safety floor* (MIN_ACTION_INTERVAL — the validation minimum; the
shipped default floor comes from the config/boot wiring, and a fleet
operator can raise it at any time via the dashboard)
is enforced by the manager (engine.py) at set time, not here: RatePolicy
itself only guarantees structural sanity so a malformed object can never
exist, even programmatically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from libzentsuite.database.models import POLICY_LEVELS

from ..errors import RatePolicyError

#: hard safety floor: no rate policy faster than this can ever be SET
#: (enforced by RateLimitManager.set_policy / set_policy_values), aligned
#: with the documented contract (INTEGRATION.md, ARCHITECTURE.md §14):
#: no configured action may pace faster than 1.5 s per action. Fleet policy
#: may gate or defer work above this floor; Discord's own per-route bucket
#: limits (from the X-RateLimit headers the library reads on write routes)
#: remain the outer runtime constraint.
MIN_ACTION_INTERVAL = 1.5

#: sanity caps (one day per action / one hour of jitter) — ported idea from
#: the audited MAX_DELAY_SECONDS: absurd values would silently wedge sends.
MAX_INTERVAL = 86400.0
MAX_JITTER = 3600.0


@dataclass(frozen=True)
class RatePolicy:
    """One pacing rule: minimum interval + optional jitter window.

    Raises:
        RatePolicyError: on any structurally invalid value (non-empty action
            string required; interval finite and 0 < interval <= 86400;
            0 <= jitter_min <= jitter_max with jitter_max <= 3600).
    """

    action: str
    interval: float
    jitter_min: float = 0.0
    jitter_max: float = 0.0

    def __post_init__(self) -> None:
        # -- action: non-empty string -----------------------------------
        if not isinstance(self.action, str):
            raise RatePolicyError(
                f"policy action must be a string, got {type(self.action).__name__}"
            )
        action = self.action.strip()
        if not action:
            raise RatePolicyError("policy action must not be empty")
        object.__setattr__(self, "action", action)

        # -- numeric fields: strict types, NaN/inf rejected explicitly ------
        # (math.isfinite catches NaN, which would otherwise poison every
        # comparison downstream — ported from the audited limiter; numeric
        # STRINGS are rejected too: validation happens at the boundary.)
        for name in ("interval", "jitter_min", "jitter_max"):
            raw = getattr(self, name)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise RatePolicyError(f"policy {name} must be a number, got {raw!r}")
            object.__setattr__(self, name, float(raw))

        if not math.isfinite(self.interval) or self.interval <= 0:
            raise RatePolicyError(
                f"policy interval must be a finite number > 0, got {self.interval!r}"
            )
        if self.interval > MAX_INTERVAL:
            raise RatePolicyError(
                f"policy interval {self.interval:g}s exceeds the {MAX_INTERVAL:g}s cap"
            )
        for name in ("jitter_min", "jitter_max"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise RatePolicyError(f"policy {name} must be a finite number >= 0, got {value!r}")
        if self.jitter_max < self.jitter_min:
            raise RatePolicyError(
                f"policy jitter_max {self.jitter_max:g}s is below jitter_min "
                f"{self.jitter_min:g}s (expected 0 <= min <= max)"
            )
        if self.jitter_max > MAX_JITTER:
            raise RatePolicyError(
                f"policy jitter_max {self.jitter_max:g}s exceeds the {MAX_JITTER:g}s cap"
            )

    def __str__(self) -> str:
        """Human form: ``"2.50s +0.10..0.50s"`` (jitter part omitted when 0)."""
        text = f"{self.interval:.2f}s"
        if self.jitter_max > 0:
            text += f" +{self.jitter_min:.2f}..{self.jitter_max:.2f}s"
        return text


def parse_scope(level: Any, owner: Any) -> tuple[str, str | None]:
    """Validate + normalize a (level, owner) policy scope.

    Rules (mirrors database.models.RatePolicyRecord semantics):

    * level must be one of ``database.models.POLICY_LEVELS``;
    * ``global`` takes NO owner (None or "" normalize to None);
    * every other level REQUIRES a non-empty owner string;
    * ``account_plugin`` owners must look like ``"account/plugin"`` —
      exactly one ``/`` with both parts non-empty.

    Returns:
        The normalized ``(level, owner)`` tuple (global -> ``("global", None)``).

    Raises:
        RatePolicyError: with a human-readable reason for any invalid scope.
    """
    if not isinstance(level, str) or level not in POLICY_LEVELS:
        raise RatePolicyError(f"unknown policy level {level!r} (use one of {list(POLICY_LEVELS)})")
    if level == "global":
        if owner not in (None, ""):
            raise RatePolicyError(
                f"global policies apply to every account — pass owner=None, got {owner!r}"
            )
        return "global", None
    if not isinstance(owner, str) or not owner.strip():
        kind = (
            "account name"
            if level == "account"
            else "plugin name"
            if level == "plugin"
            else "'account/plugin'"
        )
        raise RatePolicyError(f"policy level {level!r} requires an owner ({kind})")
    owner = owner.strip()
    if level == "account_plugin":
        parts = owner.split("/")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise RatePolicyError(
                f"account_plugin owner must look like 'account/plugin' with "
                f"both parts non-empty, got {owner!r}"
            )
        owner = f"{parts[0].strip()}/{parts[1].strip()}"
    return level, owner


def policy_from_row(row: Any | dict[str, Any]) -> tuple[str, str | None, RatePolicy]:
    """Convert one repository row into a validated ``(level, owner, policy)``.

    Rows arrive either as RatePolicyRecord-shaped objects (attribute access)
    or as plain dicts with the same keys — both are accepted so the manager
    works against any repo implementation.

    Raises:
        RatePolicyError: when the row is structurally invalid (bad level,
            bad owner shape, non-numeric interval...). Callers decide whether
            to skip the row (the manager logs + skips on load).
    """

    def _field(name: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            return row.get(name, default)
        return getattr(row, name, default)

    level, owner = parse_scope(_field("level"), _field("owner"))
    action = _field("action")
    if not isinstance(action, str) or not action.strip():
        raise RatePolicyError(f"stored policy has an invalid action {action!r}")
    policy = RatePolicy(
        action=action,
        interval=_field("interval"),
        jitter_min=_field("jitter_min", 0.0) or 0.0,
        jitter_max=_field("jitter_max", 0.0) or 0.0,
    )
    return level, owner, policy


__all__ = [
    "MAX_INTERVAL",
    "MAX_JITTER",
    "MIN_ACTION_INTERVAL",
    "RatePolicy",
    "parse_scope",
    "policy_from_row",
]
