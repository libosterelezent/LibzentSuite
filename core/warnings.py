"""Safety warnings + the hard rate floor for selfbot protection.

Two layers:
  * HARD FLOOR — ControlAPI.set_limit refuses any rule faster than
    MIN_ACTION_INTERVAL seconds per action (default rules sit exactly at it).
    Applies to BOTH the CLI and the dashboard because both go through the API.
  * WARNINGS — heuristic review of the live limiter state, surfaced in
    'warnings' (CLI), the dashboard banner, and the simulation inspector.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .rate_limiter import RateLimiterService, RateRule

#: minimum seconds between two actions of the same type per account
MIN_ACTION_INTERVAL = 1.5

#: below this interval (even if above the floor) we warn about aggressiveness
AGGRESSIVE_INTERVAL = 3.0

#: delay ranges narrower than this look robotic
NARROW_DELAY_SPREAD = 0.3


class UnsafeRateRuleError(ValueError):
    """Raised when a rule would break the selfbot safety floor."""


@dataclass
class SafetyWarning:
    """One advisement about current configuration."""

    level: str          # "info" | "warn" | "danger"
    code: str
    message: str
    account: Optional[str] = None

    def as_dict(self) -> dict:
        out = {"level": self.level, "code": self.code, "message": self.message}
        if self.account:
            out["account"] = self.account
        return out


def check_rule_safety(rule: RateRule, context: str = "") -> Optional[SafetyWarning]:
    """Interval-based verdict for one rule; None when comfortable."""
    interval = rule.window / rule.actions
    where = f" for {context}" if context else ""
    if interval < MIN_ACTION_INTERVAL:
        return SafetyWarning(
            "danger", "below_floor",
            f"rule {rule}{where} is {interval:.2f}s/action — below the "
            f"{MIN_ACTION_INTERVAL}s safety floor; refusal enforced by the API",
        )
    if interval < AGGRESSIVE_INTERVAL:
        return SafetyWarning(
            "warn", "aggressive",
            f"rule {rule}{where} averages {interval:.2f}s/action — aggressive; "
            "consider >= 3s/action with a human-like delay range",
        )
    return None


def enforce_floor(action: str, rule: RateRule, account: Optional[str] = None) -> None:
    """Raise UnsafeRateRuleError when a rule violates the hard floor.

    Used by ControlAPI.set_limit so BOTH management surfaces share the limit.
    """
    interval = rule.window / rule.actions
    if interval < MIN_ACTION_INTERVAL:
        target = f"account {account!r}" if account else "ALL accounts"
        raise UnsafeRateRuleError(
            f"rejected rule {rule} for {target}: {interval:.2f}s/action is below the "
            f"{MIN_ACTION_INTERVAL}s minimum (selfbot safety floor). "
            f"Slowest allowed example: 1/{MIN_ACTION_INTERVAL}s + a random delay."
        )


def evaluate_limiter(limiter: RateLimiterService) -> List[SafetyWarning]:
    """Review every configured action + override + delay for risk.

    AUDIT FIXES: per-account overrides with no global default are now
    reviewed (union of actions); per-account DELAY overrides and per-account
    zero-jitter are checked inside the override loop.
    """
    warnings: List[SafetyWarning] = []
    override_actions = {act for (_acct, act) in limiter._account_rules}
    override_actions |= {act for (_acct, act) in limiter._account_delays}
    for action in sorted(set(limiter._rules) | override_actions):
        default_rule = None
        try:
            default_rule = limiter.rule_for(action)
            verdict = check_rule_safety(default_rule, f"{action} (global default)")
            if verdict:
                warnings.append(verdict)
        except Exception:
            pass  # action only exists as an override — reviewed below
        delay = limiter.delay_for(action)
        if not delay.enabled:
            warnings.append(SafetyWarning(
                "warn", "no_human_delay",
                f"action {action!r} has no human-like delay range — add one "
                "(e.g. 0.80s..2.40s) to avoid metronome timing",
            ))
        elif round(delay.maximum - delay.minimum, 9) < NARROW_DELAY_SPREAD:
            warnings.append(SafetyWarning(
                "info", "narrow_delay",
                f"action {action!r} delay range {delay} is narrow — widen it "
                "for less robotic spacing",
            ))
        if default_rule is not None and default_rule.jitter <= 0:
            warnings.append(SafetyWarning(
                "info", "no_jitter",
                f"action {action!r} has no jitter on bucket waits — enable some",
            ))
        for (account, act), override in sorted(limiter._account_rules.items()):
            if act != action:
                continue
            verdict = check_rule_safety(override, f"{account}:{action}")
            if verdict:
                warnings.append(verdict)
            if override.jitter <= 0:
                warnings.append(SafetyWarning(
                    "info", "no_jitter",
                    f"account {account!r} rule {override} for {action!r} has no jitter",
                ))
        for (account, act), spec in sorted(limiter._account_delays.items()):
            if act != action:
                continue
            if not spec.enabled:
                warnings.append(SafetyWarning(
                    "warn", "no_human_delay",
                    f"account {account!r} DISABLED the human-like delay for {action!r} "
                    "(overrides the global range)",
                ))
            elif round(spec.maximum - spec.minimum, 9) < NARROW_DELAY_SPREAD:
                warnings.append(SafetyWarning(
                    "info", "narrow_delay",
                    f"account {account!r} delay range {spec} for {action!r} is narrow",
                ))
    return warnings


def format_warnings(warnings: List[SafetyWarning]) -> List[str]:
    """Warnings -> CLI/TUI lines."""
    if not warnings:
        return ["  no safety warnings — configuration looks sane"]
    icons = {"info": "[i]", "warn": "[!]", "danger": "[X]"}
    return [
        f"  {icons.get(w.level, '[?]')} {w.code:<16}{w.message}"
        for w in warnings
    ]
