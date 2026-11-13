"""Typed domain models: what a successful parse returns.

Design rules:
  * frozen dataclasses — results are immutable snapshots;
  * flat and simple — no deep nesting, every field directly meaningful;
  * ``Optional`` means *absent in the message*: a missing cooldown
    duration is ``None``, a zero-minute cooldown is ``timedelta(0)``
    (missing != zero);
  * usernames extracted from message content are kept distinct from the
    caller-supplied ``account_username`` context; one never overwrites
    the other;
  * every model carries a ``to_dict()`` for the future dashboard's JSON
    boundary without sacrificing typed fields here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .diagnostics import Diagnostics
from .enums import (
    CooldownKind,
    KakeraEventKind,
    MessageFamily,
    TargetSource,
)

__all__ = [
    "CharacterInfo",
    "CooldownInfo",
    "KakeraEvent",
    "KakeralootInfo",
    "KeyInfo",
    "MarriageInfo",
    "ParseResult",
    "TimerInfo",
    "TradeInfo",
    "WishlistInfo",
    "parse_duration_minutes",
    "parse_number",
]


def parse_number(text: str | None) -> float | None:
    """Parse Mudae's number formats: ``1,282,329`` / ``957,055,879.6``.

    Returns int when the value is integral (kakera counts), float when
    fractional (roll stacks), None for absent/blank input. Never raises
    on stray separators — returns None instead (caller decides whether
    that is a hard failure for its rule).
    """
    if text is None:
        return None
    cleaned = text.strip().replace(",", "")
    if not cleaned or cleaned in {".", "-"}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value.is_integer():
        return int(value)
    return value


def parse_duration_minutes(
    minutes: str | None = None,
    hours: str | None = None,
) -> timedelta | None:
    """Build a typed duration from captured minute/hour text groups.

    Dataset formats: ``**43** min`` → minutes="43"; ``**2h 59** min`` →
    hours="2", minutes="59". Returns None when BOTH captures are absent
    (missing duration), timedelta(0) only when the text genuinely says
    zero.
    """
    if minutes is None and hours is None:
        return None
    total = 0
    if hours is not None and str(hours).strip():
        total += int(float(str(hours).strip())) * 60
    if minutes is not None and str(minutes).strip():
        total += int(float(str(minutes).strip()))
    return timedelta(minutes=total)


@dataclass(frozen=True)
class KeyInfo:
    """One key entry inside a character embed.

    Wiki-verified (mudae.fandom.com/wiki/Keys): exactly FOUR tiers —
    bronze (1-2 keys), silver (3-5), gold (6-9), chaos (10+). Dataset
    forms::

        <:chaoskey:..> (**26**) +5% kakera value   (rolls, chaos +5%)
        <:silverkey:..> (**N**) +10% kakera value  (silver/gold +10%)
        <:chaoskey:..>  (1,476)                     (info embeds)
    """

    key_number: int
    #: tier slug from the emoji name: bronze|silver|gold|chaos
    key_type: str | None = None
    #: "+N% kakera value" percentage when present (info form: None)
    value_pct: int | None = None

    def to_dict(self) -> dict:
        return {
            "key_number": self.key_number,
            "key_type": self.key_type,
            "value_pct": self.value_pct,
        }


@dataclass(frozen=True)
class CharacterInfo:
    """A waifu/husbando character as presented by a roll/info embed."""

    #: character name — embed author name (dataset: ``author=<NAME>``)
    name: str | None = None
    #: series — first segment of the embed description
    series: str | None = None
    claims_rank: int | None = None
    likes_rank: int | None = None
    #: base kakera value (``**8,658**<:kakera:..>``)
    kakera_value: int | None = None
    #: key entries found in the description (may be empty)
    keys: tuple[KeyInfo, ...] = ()
    #: starwish indicators (``<:sw:..>`` — $sw = $starwish, wiki
    #: verified) adjacent to the series; usually 0 or 1
    starwish_count: int = 0
    #: ``Belongs to <U>`` footer → claimed/owned by that user
    claimed_by: str | None = None
    #: True when the roll is still open ("React with any emoji to
    #: claim!" suffix — live-verified server variant); None = unknown
    claimable: bool | None = None

    @property
    def is_claimed(self) -> bool | None:
        """True/False from the footer; None when no footer observed."""
        return None if self.claimed_by is None else True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "series": self.series,
            "claims_rank": self.claims_rank,
            "likes_rank": self.likes_rank,
            "kakera_value": self.kakera_value,
            "keys": [key.to_dict() for key in self.keys],
            "starwish_count": self.starwish_count,
            "claimed_by": self.claimed_by,
            "claimable": self.claimable,
        }


@dataclass(frozen=True)
class CooldownInfo:
    """One cooldown/timer observation with a typed kind.

    ``remaining`` None = duration not stated in the message (≠ zero);
    metadata carries kind-specific extras (uses per hour, rolls left,
    reaction power, marker text like ``($ku)``/``($rtu)``).
    """

    kind: CooldownKind
    remaining: timedelta | None = None
    username: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "remaining_seconds": None
            if self.remaining is None
            else int(self.remaining.total_seconds()),
            "username": self.username,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class KakeraEvent:
    """One kakera-amount event (gain, deficit, gift, bonus...)."""

    kind: KakeraEventKind
    amount: int | None = None
    #: username as extracted from the message text (not account context)
    username: str | None = None
    #: sphere amounts riding along (``**+24** <:sp:..>``)
    spheres: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "amount": self.amount,
            "username": self.username,
            "spheres": self.spheres,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class WishlistInfo:
    """Wishlist stock/refill line (``$oh/$oc/$oq/$ot`` + stock)."""

    stock: int | None = None
    refill_in: timedelta | None = None
    oh_left: int | None = None
    oc_left: int | None = None
    oq_left: int | None = None
    ot_left: int | None = None

    def to_dict(self) -> dict:
        return {
            "stock": self.stock,
            "refill_in_seconds": None
            if self.refill_in is None
            else int(self.refill_in.total_seconds()),
            "oh_left": self.oh_left,
            "oc_left": self.oc_left,
            "oq_left": self.oq_left,
            "ot_left": self.ot_left,
        }


@dataclass(frozen=True)
class KakeralootInfo:
    """$kl payout line / Kakeraloot profile headline numbers."""

    kakera_gained: int | None = None
    rolls_stacked: float | None = None
    stock: float | None = None
    #: profile embed title username (``user - Kakeraloots``)
    username: str | None = None

    def to_dict(self) -> dict:
        return {
            "kakera_gained": self.kakera_gained,
            "rolls_stacked": self.rolls_stacked,
            "stock": self.stock,
            "username": self.username,
        }


@dataclass(frozen=True)
class TradeInfo:
    """Trade / gift / give-confirmation events."""

    kind: KakeraEventKind
    amount: int | None = None
    username: str | None = None
    from_user_id: int | None = None
    to_user_id: int | None = None
    spheres: int | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "amount": self.amount,
            "username": self.username,
            "from_user_id": self.from_user_id,
            "to_user_id": self.to_user_id,
            "spheres": self.spheres,
        }


@dataclass(frozen=True)
class MarriageInfo:
    """``X and **Y** are now married!`` + bonus lines."""

    user_a: str | None = None
    character: str | None = None
    kakera_bonus: int | None = None
    tier: str | None = None
    spheres: int | None = None

    def to_dict(self) -> dict:
        return {
            "user_a": self.user_a,
            "character": self.character,
            "kakera_bonus": self.kakera_bonus,
            "tier": self.tier,
            "spheres": self.spheres,
        }


@dataclass(frozen=True)
class TimerInfo:
    """$tu-style claim/rolls status (the timers family)."""

    can_claim: bool | None = None
    claim_resets_in: timedelta | None = None
    rolls_left: int | None = None
    mk_rolls: int | None = None
    rolls_reset_in: timedelta | None = None
    can_react: bool | None = None
    react_power_pct: int | None = None

    def to_dict(self) -> dict:
        def secs(value: timedelta | None) -> int | None:
            return None if value is None else int(value.total_seconds())

        return {
            "can_claim": self.can_claim,
            "claim_resets_in_seconds": secs(self.claim_resets_in),
            "rolls_left": self.rolls_left,
            "mk_rolls": self.mk_rolls,
            "rolls_reset_in_seconds": secs(self.rolls_reset_in),
            "can_react": self.can_react,
            "react_power_pct": self.react_power_pct,
        }


@dataclass(frozen=True)
class ParseResult:
    """The public result of ``MudaeParser.parse``.

    ``family`` UNKNOWN + diagnostics means "looked like nothing we
    support" — not an error. Typed payloads are populated per family;
    ``usernames`` is cross-cutting (any family may yield one).
    ``account_username`` echoes the caller-supplied context verbatim and
    is never mixed into ``usernames``.
    """

    family: MessageFamily = MessageFamily.UNKNOWN
    matched_rules: tuple[str, ...] = ()
    sources_seen: tuple[TargetSource, ...] = ()
    character: CharacterInfo | None = None
    kakera_events: tuple[KakeraEvent, ...] = ()
    cooldowns: tuple[CooldownInfo, ...] = ()
    timer: TimerInfo | None = None
    wishlist: WishlistInfo | None = None
    kakeraloot: KakeralootInfo | None = None
    trade: TradeInfo | None = None
    marriage: MarriageInfo | None = None
    usernames: tuple[str, ...] = ()
    account_username: str | None = None
    diagnostics: Diagnostics = field(default_factory=Diagnostics)

    def to_dict(self) -> dict:
        """JSON-friendly view (future dashboard boundary)."""
        return {
            "family": self.family.value,
            "matched_rules": list(self.matched_rules),
            "sources_seen": [s.value for s in self.sources_seen],
            "character": self.character.to_dict() if self.character else None,
            "kakera_events": [e.to_dict() for e in self.kakera_events],
            "cooldowns": [c.to_dict() for c in self.cooldowns],
            "timer": self.timer.to_dict() if self.timer else None,
            "wishlist": self.wishlist.to_dict() if self.wishlist else None,
            "kakeraloot": self.kakeraloot.to_dict() if self.kakeraloot else None,
            "trade": self.trade.to_dict() if self.trade else None,
            "marriage": self.marriage.to_dict() if self.marriage else None,
            "usernames": list(self.usernames),
            "account_username": self.account_username,
            "diagnostics": self.diagnostics.to_list(),
        }
