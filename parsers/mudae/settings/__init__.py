"""Settings layer: configuration models, defaults, validation,
management and persistence for the Mudae parser engine."""

from __future__ import annotations

from .manager import (
    CompiledPattern,
    EffectiveConfig,
    MudaeParserSettingsManager,
    PreviewResult,
    SettingSummary,
    default_storage_path,
)
from .models import (
    MessageStringSetting,
    RegexPatternSetting,
    SettingBase,
    UpdateResult,
    ValidationIssue,
    ValidationReport,
)
from .storage import (
    SCHEMA_VERSION,
    JSONFileSettingsStorage,
    MemorySettingsStorage,
    ParserSettingsStorage,
)

__all__ = [
    "SCHEMA_VERSION",
    "CompiledPattern",
    "EffectiveConfig",
    "JSONFileSettingsStorage",
    "MemorySettingsStorage",
    "MessageStringSetting",
    "MudaeParserSettingsManager",
    "ParserSettingsStorage",
    "PreviewResult",
    "RegexPatternSetting",
    "SettingBase",
    "SettingSummary",
    "UpdateResult",
    "ValidationIssue",
    "ValidationReport",
    "default_storage_path",
]
