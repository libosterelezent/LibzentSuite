"""Database models: plain dataclasses crossing the persistence boundary.

These are the ONLY shapes the rest of the framework uses when talking about
persisted state. Repositories (database/repositories/*) construct/return them;
managers consume them. Keep this module dependency-free (no imports from
core.* beyond nothing) so database/ stays independently testable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: policy levels (spec §5): most specific wins-by-composition (max interval)
POLICY_LEVELS = ("global", "account", "plugin", "account_plugin")


@dataclass
class AccountRecord:
    """PERSISTENT account data (spec §1: persistent vs runtime split).

    Runtime data (client, cache, latency, uptime...) lives in
    core.account_manager.AccountRuntime and is NEVER persisted here.
    """

    id: int
    name: str
    token_ref: Optional[str] = None      # .env key; token VALUES never stored
    enabled: bool = True
    sync_group: Optional[str] = None     # accounts that act in sync (optional)
    settings: Dict[str, Any] = field(default_factory=dict)   # framework layer
    profiles: List[str] = field(default_factory=list)        # assigned profiles
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "token_ref": self.token_ref,
            "enabled": self.enabled, "sync_group": self.sync_group,
            "settings": dict(self.settings), "profiles": list(self.profiles),
            "created_at": round(self.created_at, 3),
        }


@dataclass
class SyncGroupRecord:
    """A named sync group: accounts that act in sync (shared pacing).

    MEMBERSHIP is deliberately NOT stored here: an account belongs to at
    most one group, recorded on ``accounts.sync_group`` (the group NAME —
    the membership pointer), exactly like the pre-group-table field. This
    table is the group's first-class identity + metadata only.
    """

    id: int
    name: str
    description: str = ""
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name,
            "description": self.description, "enabled": self.enabled,
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
        }


@dataclass
class ProfileRecord:
    """A named capability bundle: the plugins it permits (spec §4)."""

    name: str
    plugins: List[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "plugins": sorted(self.plugins),
            "description": self.description,
        }


@dataclass
class PluginMetadata:
    """A discovered plugin's manifest as persisted for the UI."""

    name: str
    version: str = "0.0.0"
    description: str = ""
    settings_manifest: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "version": self.version,
            "description": self.description, "settings": list(self.settings_manifest),
        }


@dataclass
class RatePolicyRecord:
    """One stored rate policy row (level + owner scope it applies to).

    level/owner combinations:
        ("global", None)                     — every account, every plugin
        ("account", "<account name>")        — one account (any plugin)
        ("plugin", "<plugin name>")          — one plugin (any account)
        ("account_plugin", "<acct>/<plugin>") — one account+plugin pair
    """

    level: str
    owner: Optional[str]
    action: str
    interval: float
    jitter_min: float = 0.0
    jitter_max: float = 0.0

    def __post_init__(self) -> None:
        if self.level not in POLICY_LEVELS:
            raise ValueError(
                f"policy level {self.level!r} not in {POLICY_LEVELS}"
            )
        if self.level == "global":
            self.owner = None
        elif not self.owner:
            raise ValueError(f"policy level {self.level!r} requires an owner")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level, "owner": self.owner, "action": self.action,
            "interval": self.interval, "jitter": [self.jitter_min, self.jitter_max],
        }


__all__ = ["AccountRecord", "SyncGroupRecord", "ProfileRecord",
           "PluginMetadata", "RatePolicyRecord", "POLICY_LEVELS"]
