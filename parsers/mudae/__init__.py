"""Mudae message parser engine (``parsers.mudae``).

Public surface::

    from parsers.mudae import MudaeParser, MudaeParserSettingsManager

``MudaeParser`` parses Discord messages into typed results; every
regex pattern and identifying message string is configurable at
runtime through ``MudaeParserSettingsManager`` (validated, atomic,
persisted under ``state/parser_settings.json``) — the seam a future
dashboard Parser Manager will consume. English defaults ship built-in
and grounded in the CuratedMudaeDataset.
"""

from __future__ import annotations

from .diagnostics import Diagnostic, Diagnostics
from .enums import (
    CaseSensitivity,
    CooldownKind,
    KakeraEventKind,
    MatchMode,
    MessageFamily,
    SettingOrigin,
    Severity,
    TargetSource,
)
from .exceptions import (
    CorruptSettingsError,
    DuplicateSettingError,
    InvalidMessageError,
    MudaeParserError,
    SettingsPersistenceError,
    UnknownSettingError,
    UnsupportedLocaleError,
)
from .models import (
    CharacterInfo,
    CooldownInfo,
    KakeraEvent,
    KakeralootInfo,
    KeyInfo,
    MarriageInfo,
    ParseResult,
    TimerInfo,
    TradeInfo,
    WishlistInfo,
)
from .parser import MudaeParser
from .settings import (
    SCHEMA_VERSION,
    EffectiveConfig,
    JSONFileSettingsStorage,
    MemorySettingsStorage,
    MessageStringSetting,
    MudaeParserSettingsManager,
    PreviewResult,
    RegexPatternSetting,
    UpdateResult,
    ValidationReport,
    default_storage_path,
)

__all__ = [
    "SCHEMA_VERSION",
    "CaseSensitivity",
    "CharacterInfo",
    "CooldownInfo",
    "CooldownKind",
    "CorruptSettingsError",
    "Diagnostic",
    "Diagnostics",
    "DuplicateSettingError",
    "EffectiveConfig",
    "InvalidMessageError",
    "JSONFileSettingsStorage",
    "KakeraEvent",
    "KakeraEventKind",
    "KakeralootInfo",
    "KeyInfo",
    "MarriageInfo",
    "MatchMode",
    "MemorySettingsStorage",
    "MessageFamily",
    "MessageStringSetting",
    "MudaeParser",
    "MudaeParserError",
    "MudaeParserSettingsManager",
    "ParseResult",
    "PreviewResult",
    "RegexPatternSetting",
    "SettingOrigin",
    "SettingsPersistenceError",
    "Severity",
    "TargetSource",
    "TimerInfo",
    "TradeInfo",
    "UnknownSettingError",
    "UnsupportedLocaleError",
    "UpdateResult",
    "ValidationReport",
    "WishlistInfo",
    "default_storage_path",
]
