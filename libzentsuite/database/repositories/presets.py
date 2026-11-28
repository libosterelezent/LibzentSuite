"""PresetsRepository — preset template persistence (W2; decisions
9/15: immutable versions under a stable preset_id, three scopes).

Persistence ONLY: schema validation is the ControlAPI layer's job
(the pinned contract); this layer stores and reads validated JSON.
``owner_id`` uses '' for the global scope (SQLite UNIQUE treats NULLs
as distinct — migration v10 documents the sentinel).
"""

from __future__ import annotations

import json
import time
from typing import Any

from libzentsuite.database.models import PresetTemplateRecord

from .base import BaseRepository, inserted_rowid

__all__ = ["PresetsRepository"]


def _record(row: Any) -> PresetTemplateRecord:
    settings = row["settings_json"]
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except (TypeError, ValueError):
            settings = {}
    return PresetTemplateRecord(
        preset_id=int(row["preset_id"]),
        scope=row["scope"],
        owner_id=row["owner_id"] or "",
        plugin=row["plugin"],
        name=row["name"],
        description=row["description"],
        version=int(row["version"]),
        schema_version=int(row["schema_version"]),
        settings=settings if isinstance(settings, dict) else {},
        archived=bool(row["archived"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


class PresetsRepository(BaseRepository):
    """Preset identity + immutable versions."""

    async def create(
        self,
        scope: str,
        owner_id: str,
        plugin: str,
        name: str,
        settings: dict[str, Any],
        *,
        description: str = "",
        schema_version: int = 1,
    ) -> PresetTemplateRecord:
        """Create a NEW preset identity (version 1). A same-scope
        owner/plugin/name identity is refused here — the name-unique
        rule is enforced at the IDENTITY level, not across version
        rows (decision 15: versions share the identity)."""
        existing = await self.latest_version(scope, owner_id, plugin, name)
        if existing is not None:
            raise ValueError(
                f"preset {plugin}/{name!r} already exists in scope {scope}:{owner_id or 'global'}"
            )
        now = time.time()
        async with self._tx(f"create preset {plugin}/{name}") as conn:
            cur = await conn.execute(
                "INSERT INTO presets (preset_id, scope, owner_id, plugin, name,"
                " description, version, schema_version, settings_json,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (
                    0,
                    scope,
                    owner_id or "",
                    plugin,
                    name,
                    description,
                    schema_version,
                    json.dumps(settings or {}),
                    now,
                    now,
                ),
            )
            row_id = inserted_rowid(cur)
            # the identity is the row id of its first version
            await conn.execute("UPDATE presets SET preset_id = ? WHERE id = ?", (row_id, row_id))
        record = await self.get(row_id)
        assert record is not None
        return record

    async def get(self, row_id: int) -> PresetTemplateRecord | None:
        row = await self._fetch_one("SELECT * FROM presets WHERE id = ?", (row_id,))
        return _record(row) if row is not None else None

    async def latest_version(
        self, scope: str, owner_id: str, plugin: str, name: str
    ) -> PresetTemplateRecord | None:
        """The newest non-archived version row of a preset identity."""
        row = await self._fetch_one(
            "SELECT * FROM presets WHERE scope = ? AND owner_id = ?"
            " AND plugin = ? AND name = ? AND archived = 0"
            " ORDER BY version DESC LIMIT 1",
            (scope, owner_id or "", plugin, name),
        )
        return _record(row) if row is not None else None

    async def version(self, preset_id: int, version: int) -> PresetTemplateRecord | None:
        """One IMMUTABLE version of a preset identity (provenance reads)."""
        row = await self._fetch_one(
            "SELECT * FROM presets WHERE preset_id = ? AND version = ?", (preset_id, version)
        )
        return _record(row) if row is not None else None

    async def list(
        self,
        *,
        scope: str | None = None,
        owner_id: str | None = None,
        plugin: str | None = None,
        include_archived: bool = False,
    ) -> list[PresetTemplateRecord]:
        sql = "SELECT * FROM presets p WHERE 1=1"
        params: list[Any] = []
        if scope is not None:
            sql += " AND scope = ?"
            params.append(scope)
        if owner_id is not None:
            sql += " AND owner_id = ?"
            params.append(owner_id or "")
        if plugin is not None:
            sql += " AND plugin = ?"
            params.append(plugin)
        if not include_archived:
            sql += " AND archived = 0"
        sql += (
            " AND version = (SELECT MAX(version) FROM presets p2"
            " WHERE p2.preset_id = p.preset_id) ORDER BY plugin, name"
        )
        rows = await self._fetch_all(sql, tuple(params))
        return [_record(r) for r in rows]

    async def publish_version(
        self,
        preset_id: int,
        settings: dict[str, Any],
        *,
        schema_version: int = 1,
        description: str | None = None,
    ) -> int:
        """Decision 15: editing creates a NEW immutable version under
        the same preset identity. Returns the new version number."""
        row = await self._fetch_one(
            "SELECT MAX(version) AS v FROM presets WHERE preset_id = ?", (preset_id,)
        )
        next_version = int(row["v"] or 0) + 1 if row is not None else 1
        now = time.time()
        async with self._tx(f"publish preset {preset_id} v{next_version}") as conn:
            await conn.execute(
                "INSERT INTO presets (preset_id, scope, owner_id, plugin, name,"
                " description, version, schema_version, settings_json,"
                " created_at, updated_at)"
                " SELECT preset_id, scope, owner_id, plugin, name,"
                " COALESCE(?, description), ?, ?, ?, ?, ?"
                " FROM presets WHERE preset_id = ?"
                " AND version = (SELECT MAX(version) FROM presets WHERE preset_id = ?)",
                (
                    description,
                    next_version,
                    schema_version,
                    json.dumps(settings or {}),
                    now,
                    now,
                    preset_id,
                    preset_id,
                ),
            )
        return next_version

    async def archive(self, preset_id: int) -> bool:
        """Decision (D.5 call): workspace deletion archives its
        templates; applied configurations are untouched either way."""
        async with self._tx(f"archive preset {preset_id}") as conn:
            cur = await conn.execute(
                "UPDATE presets SET archived = 1, updated_at = ?"
                " WHERE preset_id = ? AND archived = 0",
                (time.time(), preset_id),
            )
            return bool(cur.rowcount)
