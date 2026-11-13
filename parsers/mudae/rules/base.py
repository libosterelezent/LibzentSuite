"""Rule contract: how one message family is recognized and extracted.

A rule binds, by stable setting ID:
  * string markers  — literal classification gates (from the catalog's
    message-string settings);
  * patterns        — regex extraction sources (from the catalog's
    pattern settings);
  * a trusted handler implemented in this package (never configurable
    through persisted settings — only the patterns/strings are).

The engine (``parsers/mudae/registry.py`` + ``parser.py``) runs rules
in priority order against a normalized message; the first rule whose
gates pass and whose handler claims the message assigns the family.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..enums import MessageFamily
    from ..models import (
        CharacterInfo,
        CooldownInfo,
        KakeraEvent,
        KakeralootInfo,
        MarriageInfo,
        TimerInfo,
        TradeInfo,
        WishlistInfo,
    )

__all__ = [
    "PatternBinding",
    "Rule",
    "RuleContext",
    "RuleResult",
    "StringBinding",
]


@dataclass(frozen=True)
class PatternBinding:
    """One pattern setting a rule consumes."""

    setting_id: str
    #: False → rule still matches without it (handler degrades)
    required: bool = False
    #: "first" → only the first match is delivered; "all" → every match
    multiplicity: str = "first"


@dataclass(frozen=True)
class StringBinding:
    """One message-string marker a rule gates on.

    required markers must ALL match; at least one marker (required or
    not) must match when the rule declares any markers at all.
    """

    setting_id: str
    required: bool = False


@dataclass
class RuleResult:
    """Typed payloads a handler produces for one matched message."""

    matched: bool = False
    character: CharacterInfo | None = None
    kakera_events: tuple[KakeraEvent, ...] = ()
    cooldowns: tuple[CooldownInfo, ...] = ()
    timer: TimerInfo | None = None
    wishlist: WishlistInfo | None = None
    kakeraloot: KakeralootInfo | None = None
    trade: TradeInfo | None = None
    marriage: MarriageInfo | None = None
    usernames: tuple[str, ...] = ()


class RuleContext:
    """Read-only view the engine hands a handler: marker answers,
    pattern group-dicts, parser context."""

    __slots__ = (
        "account_username",
        "group_dicts",
        "markers",
        "raw_sources",
    )

    def __init__(
        self,
        markers: dict[str, bool],
        group_dicts: dict[str, list[dict[str, Any]]],
        account_username: str | None,
        raw_sources: dict[str, list[str]],
    ) -> None:
        self.markers = markers
        self.group_dicts = group_dicts
        self.account_username = account_username
        self.raw_sources = raw_sources

    def marker(self, setting_id: str) -> bool:
        return self.markers.get(setting_id, False)

    def groups(self, setting_id: str) -> dict[str, Any] | None:
        """First match's group dict (None when the pattern missed)."""
        found = self.group_dicts.get(setting_id)
        return found[0] if found else None

    def all_groups(self, setting_id: str) -> tuple[dict[str, Any], ...]:
        """Every match's group dict (multi-entry patterns like keys)."""
        return tuple(self.group_dicts.get(setting_id, ()))


@dataclass(frozen=True)
class Rule:
    """One registered family rule (trusted, code-defined)."""

    rule_id: str
    family: MessageFamily
    handler: Callable[[RuleContext], RuleResult]
    patterns: tuple[PatternBinding, ...] = ()
    strings: tuple[StringBinding, ...] = ()
    #: lower runs first
    priority: int = 100
    #: human-facing note for diagnostics/catalog
    description: str = ""
