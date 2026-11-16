"""Encrypted-credential repository (spec §12/§13).

Stores Fernet ciphertext ONLY. The key lives outside the database (see
core.credential_service) — a stolen DB yields no usable tokens. Plaintext
never enters this layer: callers hand ciphertext in and get ciphertext
out; decryption is the CredentialService's controlled job.
"""

from __future__ import annotations

import time

from .base import BaseRepository


class CredentialsRepository(BaseRepository):
    """CRUD over the ``credentials`` table (ciphertext blobs)."""

    async def store(self, account: str, ciphertext: bytes) -> None:
        """Insert or replace the stored ciphertext for an account."""
        now = time.time()
        async with self._tx(f"store credential for {account!r}") as conn:
            await conn.execute(
                """INSERT INTO credentials (account, ciphertext, created_at, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(account) DO UPDATE SET
                     ciphertext=excluded.ciphertext, updated_at=excluded.updated_at""",
                (account, ciphertext, now, now),
            )

    async def load(self, account: str) -> bytes | None:
        """The stored ciphertext, or None when the account has none."""
        row = await self._fetch_one(
            "SELECT ciphertext FROM credentials WHERE account=?", (account,)
        )
        if row is None:
            return None
        value = row["ciphertext"]
        return bytes(value) if not isinstance(value, bytes) else value

    async def delete(self, account: str) -> bool:
        """Remove the credential; True when one existed."""
        # the cursor closes before the tx commits — an unclosed one could
        # pin a read snapshot (SQLITE_BUSY class)
        async with (
            self._tx(f"delete credential for {account!r}") as conn,
            conn.execute("DELETE FROM credentials WHERE account=?", (account,)) as cur,
        ):
            return cur.rowcount > 0

    async def accounts_with_credential(self) -> list:
        """Account names that have a stored credential (status display)."""
        rows = await self._fetch_all("SELECT account FROM credentials ORDER BY account")
        return [row["account"] for row in rows]


__all__ = ["CredentialsRepository"]
