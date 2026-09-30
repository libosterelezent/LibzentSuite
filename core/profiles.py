"""Profile Manager: named safety profiles applied per account.

A profile bundles conservative behavior: per-action rate rules, human-like
delay ranges, and send-lane spacing. Applying a profile to an account sets
its per-account overrides in the limiter (delays + rules) and the lane
spacing — all through ControlAPI so CLI and dashboard share the exact path.

Built-in profiles ship conservative (user spec: 1.5s floor + random delay):
  conservative : 3/12s per action, delay 1.20..3.00s, lane 2.0s
  balanced     : 5/12s per action, delay 0.80..2.40s, lane 1.5s
Custom profiles can be defined and persisted (DB kv 'profiles').
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("slaughtermudae.profiles")

BUILTIN_PROFILES: Dict[str, Dict[str, Any]] = {
    "conservative": {
        "description": "slow and very human — recommended for new accounts",
        "rules": {"message": "3/12s", "dm": "3/12s"},
        "delays": {"message": [1.2, 3.0], "dm": [1.5, 3.5]},
        "prefixrate": 2.0,
    },
    "balanced": {
        "description": "default pacing — sits at the 1.5s/action floor",
        "rules": {"message": "5/12s", "dm": "5/12s"},
        "delays": {"message": [0.8, 2.4], "dm": [1.0, 2.6]},
        "prefixrate": 1.5,
    },
}


@dataclass
class Profile:
    name: str
    description: str = ""
    rules: Dict[str, str] = field(default_factory=dict)
    delays: Dict[str, List[float]] = field(default_factory=dict)
    prefixrate: float = 1.5
    builtin: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "description": self.description,
            "rules": dict(self.rules), "delays": {k: list(v) for k, v in self.delays.items()},
            "prefixrate": self.prefixrate, "builtin": self.builtin,
        }


class ProfileError(ValueError):
    """Raised for invalid profile definitions."""


class ProfileManager:
    """Registry of safety profiles (built-ins + DB-persisted customs)."""

    def __init__(self, database: Optional[Any] = None) -> None:
        self._db = database
        self._profiles: Dict[str, Profile] = {}
        for name, spec in BUILTIN_PROFILES.items():
            self._profiles[name] = Profile(
                name=name, description=spec["description"], rules=dict(spec["rules"]),
                delays={k: list(v) for k, v in spec["delays"].items()},
                prefixrate=float(spec["prefixrate"]), builtin=True,
            )
        self._load_custom()

    def _load_custom(self) -> None:
        if self._db is None:
            return
        raw = self._db.kv_get("profiles")
        if not raw:
            return
        try:
            for spec in json.loads(raw):
                try:
                    self._profiles[spec["name"]] = Profile(
                        name=spec["name"], description=spec.get("description", ""),
                        rules=dict(spec.get("rules") or {}),
                        delays={k: list(v) for k, v in (spec.get("delays") or {}).items()},
                        prefixrate=float(spec.get("prefixrate", 1.5)),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    LOG.warning("skipping invalid stored profile: %s", exc)
        except json.JSONDecodeError:
            LOG.warning("stored profiles unreadable; using built-ins only")

    def _persist_custom(self) -> None:
        if self._db is None:
            return
        customs = [
            p.as_dict() for p in self._profiles.values() if not p.builtin
        ]
        self._db.kv_set("profiles", json.dumps(customs))

    # ------------------------------------------------------------------- api
    def get(self, name: str) -> Optional[Profile]:
        return self._profiles.get(name)

    def all(self) -> List[Profile]:
        return sorted(self._profiles.values(), key=lambda p: p.name)

    def manifests(self) -> List[Dict[str, Any]]:
        return [p.as_dict() for p in self.all()]

    def set_profile(self, name: str, *, rules: Optional[Dict[str, str]] = None,
                    delays: Optional[Dict[str, List[float]]] = None,
                    prefixrate: float = 1.5, description: str = "") -> Profile:
        """Create/update a CUSTOM profile (built-ins cannot be overwritten)."""
        if name in BUILTIN_PROFILES:
            raise ProfileError(f"{name!r} is a built-in profile and cannot be overwritten")
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ProfileError(f"profile name {name!r} must be alphanumeric/-/_")
        profile = Profile(
            name=name, rules=dict(rules or {}), delays={k: list(v) for k, v in (delays or {}).items()},
            prefixrate=float(prefixrate), description=description,
        )
        self._profiles[name] = profile
        self._persist_custom()
        return profile

    def delete_profile(self, name: str) -> bool:
        if name in BUILTIN_PROFILES:
            raise ProfileError(f"{name!r} is a built-in profile")
        if self._profiles.pop(name, None) is None:
            return False
        self._persist_custom()
        return True

    def resolve(self, name: str) -> Profile:
        profile = self._profiles.get(name)
        if profile is None:
            raise ProfileError(f"unknown profile {name!r} (defined: {sorted(self._profiles)})")
        return profile
