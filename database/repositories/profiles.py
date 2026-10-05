"""ProfilesRepository — named capability bundles (spec §4).

A profile is a set of plugin names an account may run, plus a description.
Rows live in ``profiles`` (name/description) and ``profile_plugins``
(membership). Ported from the audited old roles code: plugin lists are
deduplicated and sorted on save, and deleting a profile also drops its
memberships and any account assignments.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from core.errors import DatabaseError

from database.models import ProfileRecord

from .base import BaseRepository

__all__ = ["ProfilesRepository"]


class ProfilesRepository(BaseRepository):
    """CRUD for profiles, keyed by profile name."""

    async def upsert(self, name: str, plugins: Iterable[str], description: str = "") -> None:
        """Create or fully replace a profile (one transaction).

        The plugin list is deduplicated and sorted — canonical form on disk
        keeps equality checks stable no matter how the caller ordered it.
        An empty plugin list is allowed at this layer; ProfileManager is the
        one that rejects meaningless empty profiles.

        Raises DatabaseError for an empty/non-string name.
        """
        if not isinstance(name, str) or not name.strip():
            raise DatabaseError("profile name must be a non-empty string")
        name = name.strip()
        cleaned = sorted({str(p) for p in (plugins or [])})
        async with self._tx(f"upsert profile {name!r}") as conn:
            await conn.execute(
                """INSERT INTO profiles (name, description) VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET description = excluded.description""",
                (name, description or ""),
            )
            await conn.execute("DELETE FROM profile_plugins WHERE profile = ?", (name,))
            await conn.executemany(
                "INSERT INTO profile_plugins (profile, plugin) VALUES (?, ?)",
                [(name, plugin) for plugin in cleaned],
            )

    async def delete(self, name: str) -> bool:
        """Delete a profile, its plugin memberships AND its account
        assignments; True when the profile existed."""
        async with self._tx(f"delete profile {name!r}") as conn:
            async with conn.execute(
                "DELETE FROM profiles WHERE name = ?", (name,)
            ) as cur:
                existed = cur.rowcount > 0
            await conn.execute("DELETE FROM profile_plugins WHERE profile = ?", (name,))
            await conn.execute("DELETE FROM account_profiles WHERE profile = ?", (name,))
        return existed

    async def get(self, name: str) -> Optional[ProfileRecord]:
        """One profile with its sorted plugin list (None when unknown)."""
        row = await self._fetch_one("SELECT name, description FROM profiles WHERE name = ?", (name,))
        if row is None:
            return None
        plugins = [
            r["plugin"]
            for r in await self._fetch_all(
                "SELECT plugin FROM profile_plugins WHERE profile = ? ORDER BY plugin", (name,)
            )
        ]
        return ProfileRecord(name=row["name"], plugins=plugins, description=row["description"])

    async def list(self) -> List[ProfileRecord]:
        """All profiles ordered by name, each with its sorted plugins."""
        rows = await self._fetch_all("SELECT name, description FROM profiles ORDER BY name")
        if not rows:
            return []
        memberships = {}
        for row in await self._fetch_all("SELECT profile, plugin FROM profile_plugins ORDER BY plugin"):
            memberships.setdefault(row["profile"], []).append(row["plugin"])
        return [
            ProfileRecord(
                name=row["name"],
                plugins=memberships.get(row["name"], []),
                description=row["description"],
            )
            for row in rows
        ]
