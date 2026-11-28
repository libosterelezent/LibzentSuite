"""legacy_fold — P7's one-time migration helper.

The two-level model's migration contract
(docs/design/data-management-adoption.md): every legacy
``(account, plugin, template-name)`` binding becomes ONE
account_plugin_profiles instance — the template's settings body copied
verbatim (the reserved ``channels`` key stays IN the body: the profile
owns its channels), the active-profile pointer landing on it. Bindings
whose template row was deleted fold to an EMPTY instance (identity
preserved; the pointer must never dangle). Unbound templates fold to
nothing — they were never an account's configuration.

Idempotent by construction: a binding whose instance already exists is
skipped, so the boot migration (v15) and any manual re-run converge.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from libzentsuite.core.errors import DatabaseError

if TYPE_CHECKING:
    from libzentsuite.database.db import Database

__all__ = ["fold_legacy_profiles"]


async def fold_legacy_profiles(db: Database) -> dict[str, int]:
    """Fold every legacy binding into an account-owned instance.

    Returns ``{"folded": n, "skipped": s, "dangling": d}`` — folded
    counts created instances (dangling bindings included), skipped
    counts bindings whose instance already existed, dangling counts
    bindings whose template row no longer exists.
    """
    conn = db.connection
    rows = await conn.fetch_all(
        "SELECT ap.account, ap.plugin, ap.profile FROM account_plugins ap"
        " WHERE ap.profile IS NOT NULL AND ap.profile != ''"
        " ORDER BY ap.account, ap.plugin"
    )
    templates: dict[tuple[str, str], dict[str, Any]] = {}
    for tpl in await conn.fetch_all("SELECT plugin, name, settings_json FROM plugin_profiles"):
        try:
            body = json.loads(tpl["settings_json"] or "{}")
        except ValueError:
            body = {}
        templates[(tpl["plugin"], tpl["name"])] = body if isinstance(body, dict) else {}

    repo = db.account_plugin_profiles
    folded = skipped = dangling = 0
    now = time.time()
    async with conn.write() as tx:
        for row in rows:
            account, plugin, name = row["account"], row["plugin"], row["profile"]
            if await repo.exists(account, plugin, name):
                skipped += 1
                continue
            body = templates.get((plugin, name))
            if body is None:
                dangling += 1
                body = {}
            try:
                await tx.execute(
                    "INSERT INTO account_plugin_profiles"
                    " (account, plugin, name, description, settings_json,"
                    "  schema_version, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'folded from the legacy template', ?, 1, ?, ?)",
                    (account, plugin, name, json.dumps(body), now, now),
                )
            except Exception as exc:  # pragma: no cover - defensive
                raise DatabaseError(
                    f"legacy fold failed for {account}/{plugin}/{name}: {exc}"
                ) from exc
            await tx.execute(
                "INSERT INTO account_plugin_active_profile"
                " (account, plugin, profile, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(account, plugin) DO UPDATE SET"
                " profile=excluded.profile, updated_at=excluded.updated_at",
                (account, plugin, name, now),
            )
            folded += 1
    return {"folded": folded, "skipped": skipped, "dangling": dangling}
