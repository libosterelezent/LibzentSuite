"""AccountPluginsRepository — direct account→plugin assignments.

One row in ``account_plugins`` means "this account runs this plugin"
(the NEW model: Account -> Plugins, no profile in between). The profile
tables (``profiles`` / ``profile_plugins`` / ``account_profiles``) stay
untouched for a future plugin-specific profile system and are neither
read nor written here.

This layer is persistence ONLY: it does NOT validate that the account or
the plugin exists (the manager above owns that), and it performs no
cascades — cleaning up an account's rows when the account is deleted is
the account-delete path's job, exactly like the other name-keyed
per-account tables (``account_profiles``, ``plugin_settings``, ...).
"""
from __future__ import annotations

from typing import Dict, Iterable, List

from .base import BaseRepository

__all__ = ["AccountPluginsRepository"]


class AccountPluginsRepository(BaseRepository):
    """Direct plugin assignments, keyed by (account name, plugin name)."""

    async def assign_plugin(self, account: str, plugin: str) -> None:
        """Attach a plugin to an account (idempotent — re-assign is a no-op).

        INSERT OR IGNORE against the composite PRIMARY KEY: a duplicate
        assignment writes nothing and raises nothing. Whether the account
        or the plugin EXISTS is not checked here — that validation is the
        calling manager's job, not persistence's.
        """
        async with self._tx(f"assign plugin {plugin!r} to account {account!r}") as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO account_plugins (account, plugin)"
                " VALUES (?, ?)",
                (account, plugin),
            )

    async def unassign_plugin(self, account: str, plugin: str) -> None:
        """Detach a plugin from an account (idempotent; never raises).

        Deleting a row that is not there is NOT an error — the end state
        ("plugin not assigned") already holds.
        """
        async with self._tx(
            f"unassign plugin {plugin!r} from account {account!r}"
        ) as conn:
            await conn.execute(
                "DELETE FROM account_plugins WHERE account = ? AND plugin = ?",
                (account, plugin),
            )

    async def plugins_of(self, account: str) -> List[str]:
        """Sorted plugin names assigned to this account ([] when none —
        including when the account itself is unknown to the accounts
        table, which this layer never consults)."""
        return [
            row["plugin"]
            for row in await self._fetch_all(
                "SELECT plugin FROM account_plugins WHERE account = ?"
                " ORDER BY plugin",
                (account,),
            )
        ]

    async def set_plugins(self, account: str, plugins: Iterable[str]) -> List[str]:
        """FULL replacement: the account ends up with exactly ``plugins``.

        Delete-then-insert in ONE transaction, so a concurrent reader
        never observes a half-replaced set. The list is deduplicated and
        canonically sorted before storing — canonical form on disk keeps
        equality checks stable no matter how the caller ordered it.
        Returns the stored sorted list (empty input clears the account).
        """
        cleaned = sorted({p for p in (plugins or [])})
        async with self._tx(f"set plugins of account {account!r}") as conn:
            await conn.execute(
                "DELETE FROM account_plugins WHERE account = ?", (account,)
            )
            await conn.executemany(
                "INSERT INTO account_plugins (account, plugin) VALUES (?, ?)",
                [(account, plugin) for plugin in cleaned],
            )
        return cleaned

    async def all_assignments(self) -> Dict[str, List[str]]:
        """Every assignment as ``{account: [plugin, ...]}`` for boot restore.

        One bulk read. Accounts with ZERO plugins are OMITTED (they have
        no rows): consumers must treat a missing key as "no plugins",
        i.e. read ``assignments.get(account, [])`` — never index the map
        directly. Each account's list is sorted; keys iterate in sorted
        order.
        """
        assignments: Dict[str, List[str]] = {}
        for row in await self._fetch_all(
            "SELECT account, plugin FROM account_plugins"
            " ORDER BY account, plugin"
        ):
            assignments.setdefault(row["account"], []).append(row["plugin"])
        return assignments

    # ------------------------------------------------------- profile bindings
    # account_plugins.profile (migration v7) binds an assignment to a
    # plugin setting profile: NULL = plugin defaults. Kept SEPARATE from
    # the assignment methods above so their return shapes stay frozen —
    # profile-aware consumers use bindings()/all_bindings() instead.
    async def set_profile(
        self, account: str, plugin: str, profile: Optional[str]
    ) -> bool:
        """Bind the assignment to a profile (None = clear, back to defaults).

        UPDATE-only and idempotent: setting the same binding again writes
        the same row-state, and an assignment row that does not exist is
        a clean no-op returning False — this layer never CREATES an
        assignment implicitly (binding requires assignment is the calling
        manager's rule). Whether the profile NAME exists is not checked
        here either: profile rows belong to PluginProfilesRepository.
        """
        async with self._tx(
            f"set plugin profile of {account!r}/{plugin!r} to {profile!r}"
        ) as conn:
            async with conn.execute(
                "UPDATE account_plugins SET profile = ?"
                " WHERE account = ? AND plugin = ?",
                (profile, account, plugin),
            ) as cur:
                return cur.rowcount > 0

    async def bindings(self, account: str) -> Dict[str, Optional[str]]:
        """``{plugin: profile name or None}`` for every plugin assigned to
        the account ({} when none — including when the account itself is
        unknown to the accounts table, which this layer never consults)."""
        return {
            row["plugin"]: row["profile"]
            for row in await self._fetch_all(
                "SELECT plugin, profile FROM account_plugins"
                " WHERE account = ? ORDER BY plugin",
                (account,),
            )
        }

    async def all_bindings(self) -> Dict[str, Dict[str, Optional[str]]]:
        """Every binding as ``{account: {plugin: profile or None}}``.

        One bulk read for boot restore, shaped like all_assignments but
        carrying the profile column; accounts with zero plugins are
        OMITTED by the same convention (``bindings.get(account, {})``).
        Keys iterate in sorted order.
        """
        out: Dict[str, Dict[str, Optional[str]]] = {}
        for row in await self._fetch_all(
            "SELECT account, plugin, profile FROM account_plugins"
            " ORDER BY account, plugin"
        ):
            out.setdefault(row["account"], {})[row["plugin"]] = row["profile"]
        return out
