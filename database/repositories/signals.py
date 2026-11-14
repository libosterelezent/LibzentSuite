"""SignalsRepository — the durable outbox over the v10 ``signals``
table (W4; decision 12).

Persistence ONLY: acceptance semantics live in
core.workspace.signals.SignalOutbox; matching against chain bindings
is the chain runtime's job. delivery_state: pending | delivered |
dead — an accepted signal is one of the three forever (retention
purges delivered/dead rows on a schedule).
"""

from __future__ import annotations

import json
import time

from core.workspace.models import SignalIdentity

from .base import BaseRepository

__all__ = ["SignalsRepository"]


def _identity(row) -> SignalIdentity:
    payload = row["payload_json"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    return SignalIdentity(
        signal_id=row["signal_id"],
        account=row["account"],
        plugin=row["plugin"],
        name=row["name"],
        payload=payload if isinstance(payload, dict) else {},
        correlation_id=row["correlation_id"] or "",
        created_at=float(row["created_at"]),
    )


class SignalsRepository(BaseRepository):
    """Accept, list pending, ack, dead-letter, purge."""

    async def insert(self, signal: SignalIdentity) -> None:
        """Durable acceptance — the row EXISTS before accept() returns."""
        async with self._tx(f"accept signal {signal.namespaced}") as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO signals"
                " (signal_id, account, plugin, name, payload_json,"
                "  correlation_id, created_at, delivery_state, attempts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0)",
                (
                    signal.signal_id,
                    signal.account,
                    signal.plugin,
                    signal.name,
                    json.dumps(signal.payload),
                    signal.correlation_id,
                    signal.created_at,
                ),
            )

    async def pending(self, limit: int) -> list[SignalIdentity]:
        rows = await self._fetch_all(
            "SELECT * FROM signals WHERE delivery_state = 'pending' ORDER BY created_at LIMIT ?",
            (int(limit),),
        )
        return [_identity(r) for r in rows]

    async def mark_delivered(self, signal_id: str, attempts: int) -> None:
        del attempts  # the counter stays as history on the row
        async with self._tx(f"ack signal {signal_id}") as conn:
            await conn.execute(
                "UPDATE signals SET delivery_state = 'delivered',"
                " acked_at = ?, last_error = '' WHERE signal_id = ?",
                (time.time(), signal_id),
            )

    async def mark_dead(self, signal_id: str, attempts: int, error: str) -> None:
        del attempts
        async with self._tx(f"dead-letter signal {signal_id}") as conn:
            await conn.execute(
                "UPDATE signals SET delivery_state = 'dead', last_error = ? WHERE signal_id = ?",
                (error, signal_id),
            )

    async def record_attempt(self, signal_id: str, error: str) -> None:
        async with self._tx(f"signal attempt {signal_id}") as conn:
            await conn.execute(
                "UPDATE signals SET attempts = attempts + 1, last_error = ? WHERE signal_id = ?",
                (error, signal_id),
            )

    async def attempts_of(self, signal_id: str) -> int | None:
        row = await self._fetch_one(
            "SELECT attempts FROM signals WHERE signal_id = ?", (signal_id,)
        )
        return int(row["attempts"]) if row is not None else None

    async def purge_delivered_before(self, cutoff: float) -> int:
        """Retention: delivered/dead rows older than the cutoff die."""
        async with self._tx("purge signals") as conn:
            cur = await conn.execute(
                "DELETE FROM signals WHERE delivery_state IN"
                " ('delivered', 'dead') AND created_at < ?",
                (cutoff,),
            )
            return int(cur.rowcount or 0)

    async def signal(self, signal_id: str) -> SignalIdentity | None:
        row = await self._fetch_one("SELECT * FROM signals WHERE signal_id = ?", (signal_id,))
        return _identity(row) if row is not None else None
