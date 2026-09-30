"""Roles: name a bundle of plugin capabilities, assign roles to selfbots.

Concept (user spec): "selfbotA given a role to Roll, selfbotB/C given a role
to Snipe" — Roll and Snipe are (future) plugins; the Role is the control
point. An account with roles may ONLY run the plugins its roles allow
(union across its roles). An account with NO roles keeps its configured
plugin list untouched (backwards compatible).

Roles take effect when the account is (re)started; assignments persist in
the database and are editable from both the CLI and the dashboard.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

LOG = logging.getLogger("slaughtermudae.roles")

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class RoleError(ValueError):
    """Raised for invalid role definitions."""


@dataclass
class Role:
    """A named capability bundle listing the plugins it permits."""

    name: str
    plugins: List[str] = field(default_factory=list)
    description: str = ""

    def __post_init__(self) -> None:
        if not NAME_RE.match(self.name or ""):
            raise RoleError(f"role name {self.name!r} must match {NAME_RE.pattern}")

    def allows(self, plugin: str) -> bool:
        return plugin in self.plugins


class RoleRegistry:
    """Runtime view over the persisted role table (database is the source)."""

    def __init__(self, database: Optional[Any] = None) -> None:
        self._db = database
        self._cache: Dict[str, Role] = {}
        self.reload()

    def reload(self) -> None:
        """Re-read roles from the database."""
        self._cache.clear()
        if self._db is None:
            return
        for name, info in self._db.roles().items():
            self._cache[name] = Role(
                name=name, plugins=list(info["plugins"]), description=info["description"]
            )

    def get(self, name: str) -> Optional[Role]:
        return self._cache.get(name)

    def all(self) -> Dict[str, Role]:
        return dict(self._cache)

    def manifests(self) -> List[dict]:
        """JSON-safe role list for CLI/dashboard."""
        return [
            {"name": r.name, "description": r.description, "plugins": sorted(r.plugins)}
            for r in sorted(self._cache.values(), key=lambda r: r.name)
        ]

    def set_role(self, name: str, plugins: Iterable[str], description: str = "") -> Role:
        """Create/update a role (persisted)."""
        plugin_list = sorted({p.strip() for p in plugins if p and p.strip()})
        if not plugin_list:
            raise RoleError(f"role {name!r} must allow at least one plugin")
        role = Role(name=name, plugins=plugin_list, description=description)
        if self._db is not None:
            self._db.upsert_role(name, plugin_list, description)
        self._cache[name] = role
        return role

    def delete_role(self, name: str) -> bool:
        """Remove a role; accounts holding it simply lose that grant."""
        if self._db is not None:
            self._db.delete_role(name)
        existed = self._cache.pop(name, None) is not None
        return existed

    def allowed_plugins(self, roles: Iterable[str]) -> Optional[Set[str]]:
        """Union of plugin grants across the given role names.

        Unknown role names are logged and ignored. Returns None when no KNOWN
        role matches (=> no restriction applied, preserving old behavior).
        """
        allowed: Set[str] = set()
        seen_known = False
        for name in roles or []:
            role = self._cache.get(name)
            if role is None:
                LOG.warning("account references unknown role %r (ignored)", name)
                continue
            seen_known = True
            allowed.update(role.plugins)
        return allowed if seen_known else None
