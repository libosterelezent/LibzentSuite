"""SyncGroupManager — the application-level owner of sync group membership.

Division of labour (the managers cooperate, none absorbs another):

  * SyncGroupManager (this)  — persistent group rows + which account is in
                               which group; the ONLY writer of membership.
  * RateLimitManager         — runtime PACING. Grouped accounts serialize on
                               the shared ``group:<name>`` clock exactly as
                               before; this manager only keeps the engine's
                               account->group metadata in sync through the
                               existing ``set_account_group`` API.
  * AccountManager           — account lifecycle; delegates the group bits
                               of add/remove/restore here when wired.

Persistence model (see database/repositories/sync_groups.py): group rows in
``sync_groups`` (id = stable identity, name = unique pointer), membership
on ``accounts.sync_group``. An account is in AT MOST one group.

Boot relationship: AccountManager.load() -> sync_runtime() here -> the
limiter mapping. Every membership mutation persists FIRST (repository
transaction), then synchronizes the limiter mapping — persist+sync run
under one manager lock so concurrent moves of the same account (A->Alpha
racing A->Beta) always leave the database and the runtime mapping agreeing.

Legacy compatibility: names that predate first-class groups (or arrive via
the legacy ``accounts sync_group`` field on creation) are ADOPTED verbatim
— ``sync_membership`` get-or-creates the group row, exactly like migration
v5 backfills one group per distinct legacy name.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal, Protocol, overload

from ..errors import SyncGroupError
from ..names import GROUP_NAME_RE as NAME_RE

if TYPE_CHECKING:
    import builtins

    from database.models import AccountRecord, SyncGroupRecord

__all__ = ["NAME_RE", "SyncGroupManager"]

#: what the group CRUD/membership methods accept as the group reference:
#: the stable int id or the unique name.
GroupRef = int | str


class GroupsRepo(Protocol):
    """The sync-groups persistence surface this manager consumes
    (satisfied structurally by ``database.repositories.SyncGroupsRepository``)."""

    async def create(
        self, name: str, description: str = "", *, enabled: bool = True
    ) -> SyncGroupRecord: ...
    async def get(self, group_id: int) -> SyncGroupRecord | None: ...
    async def get_by_name(self, name: str) -> SyncGroupRecord | None: ...
    async def list(self) -> builtins.list[SyncGroupRecord]: ...
    async def update(
        self,
        group_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> SyncGroupRecord | None: ...
    async def delete(self, group_id: int) -> bool: ...
    async def members(self, group_name: str) -> builtins.list[str]: ...
    async def membership_counts(self) -> dict[str, int]: ...
    async def account_group_name(self, account: str) -> str | None: ...
    async def set_account_group(self, account: str, group: str | None) -> None: ...
    async def detach_members(self, group_name: str) -> builtins.list[str]: ...


class AccountsReader(Protocol):
    """The read surface over accounts this manager needs (existence
    validation + boot restore); satisfied by ``AccountsRepository``."""

    async def list(self) -> builtins.list[AccountRecord]: ...


class PacingRegistrar(Protocol):
    """The limiter's runtime membership surface (account -> shared group
    clock). Accessed defensively at runtime — doubles missing a member
    simply no-op (see ``_sync_runtime``)."""

    def set_account_group(self, account: str, group: str | None) -> None: ...
    def account_groups(self) -> dict[str, str]: ...


class SyncGroupManager:
    """CRUD + membership for sync groups; keeps the limiter metadata true."""

    NAME_RE = NAME_RE

    def __init__(
        self,
        groups_repo: GroupsRepo,
        logger: logging.Logger | None = None,
        *,
        accounts_repo: AccountsReader | None = None,
        limiter: PacingRegistrar | None = None,
    ) -> None:
        """``groups_repo``: SyncGroupsRepository; ``accounts_repo`` (optional)
        enables account validation + boot restore; ``limiter`` (optional,
        duck-typed) receives the runtime account->group mapping via its
        ``set_account_group``. The constructor does NO I/O — await
        :meth:`load` at boot.
        """
        self._repo = groups_repo
        self._accounts = accounts_repo
        self._limiter = limiter
        self._logger = logger or logging.getLogger("libzent.sync_groups")
        #: name -> SyncGroupRecord (cache; refresh via load()/mutators)
        self._cache: dict[str, SyncGroupRecord] = {}
        #: serializes persist+runtime-sync per membership change so the
        #: database and the limiter mapping can never disagree (the repo's
        #: own write lock serializes the SQL; this extends the critical
        #: section across the limiter sync).
        self._membership_lock = asyncio.Lock()

    # ---------------------------------------------------------------- boot
    async def load(self) -> None:
        """Populate the group cache from the repository (boot path).

        Best-effort: a failure logs and leaves the
        cache empty — reads then fall back to read-through repo queries.
        """
        try:
            for record in await self._repo.list() or []:
                self._cache[record.name] = record
        except Exception:
            self._logger.exception("could not load sync groups from repository")

    async def sync_runtime(self) -> int:
        """Boot restore: push every PERSISTED membership into the limiter.

        Called once by AccountManager.load() (wired stacks) after accounts
        are restored — the single place group restoration happens. Accounts
        whose legacy ``sync_group`` value has no group row yet are adopted
        (get-or-create), keeping hand-written/config-dir values functional.
        Returns how many memberships were registered.
        """
        if self._accounts is None:
            return 0
        try:
            records = await self._accounts.list() or []
        except Exception:
            self._logger.exception("could not read accounts for group restore")
            return 0
        restored = 0
        for record in records:
            group = getattr(record, "sync_group", None)
            if not group:
                continue
            await self.sync_membership(record.name, group)
            restored += 1
        return restored

    # ---------------------------------------------------------------- CRUD
    async def create_group(
        self, name: str, description: str = "", *, enabled: bool = True
    ) -> dict[str, Any]:
        """Create a group (may exist with zero members).

        Raises:
            SyncGroupError: bad name (must match NAME_RE), or duplicate.
        """
        name = self._validated_name(name)
        if await self._repo_get_by_name(name) is not None:
            raise SyncGroupError(f"sync group {name!r} already exists")
        record = await self._repo.create(name, description, enabled=bool(enabled))
        self._cache[record.name] = record
        self._logger.info("sync group %s created (enabled=%s)", name, enabled)
        return record.to_dict()

    async def get_group(self, group: GroupRef) -> dict[str, Any] | None:
        """One group by id or name (None when unknown)."""
        record = await self._resolve(group, required=False)
        return record.to_dict() if record is not None else None

    async def get_group_by_name(self, name: str) -> dict[str, Any] | None:
        """One group by name (None when unknown)."""
        record = await self._repo_get_by_name((name or "").strip())
        return record.to_dict() if record is not None else None

    async def list_groups(self) -> list[dict[str, Any]]:
        """Every group as ``{"id","name","description","enabled",
        "member_count","created_at","updated_at"}``, sorted by name.

        Read-through (refreshes the cache on read).
        """
        try:
            records = await self._repo.list() or []
        except Exception:
            self._logger.exception("could not list sync groups")
            records = list(self._cache.values())
        counts = await self._membership_counts()
        for record in records:
            self._cache[record.name] = record
        return [
            {**record.to_dict(), "member_count": counts.get(record.name, 0)}
            for record in sorted(records, key=lambda r: r.name)
        ]

    async def update_group(
        self,
        group: GroupRef,
        *,
        name: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Update the given fields; a rename also rewrites every member's
        pointer (repository transaction) and re-syncs the runtime mapping,
        so no account stays attached to the OLD ``group:<name>`` clock.

        Raises:
            SyncGroupError: unknown group, bad/duplicate new name.
        """
        record = await self._resolve(group)
        if name is not None and name != record.name:
            new_name = self._validated_name(name)
            if await self._repo_get_by_name(new_name) is not None:
                raise SyncGroupError(f"sync group {new_name!r} already exists")
        updated = await self._repo.update(
            record.id, name=name, description=description, enabled=enabled
        )
        if updated is None:  # vanished mid-update
            raise SyncGroupError(f"sync group {group!r} no longer exists")
        self._cache.pop(record.name, None)
        self._cache[updated.name] = updated
        if updated.name != record.name:
            # members now point at the new name; move their runtime pacing
            # scope with them (the engine drops the old group's shared
            # stamps once the last member has moved)
            members = await self._members(updated.name)
            for member in members:
                self._sync_runtime(member, updated.name)
            self._logger.info(
                "sync group %s renamed to %s (%d member(s) moved)",
                record.name,
                updated.name,
                len(members),
            )
        else:
            self._logger.info("sync group %s updated", updated.name)
        return updated.to_dict()

    async def delete_group(
        self, group: GroupRef, *, detach_members: bool = False
    ) -> dict[str, Any]:
        """Delete a group. Its ACCOUNTS are never deleted.

        A group with members is refused (the members' pacing membership
        would dangle) unless ``detach_members`` explicitly clears it first;
        detached members return to per-account pacing.

        Raises:
            SyncGroupError: unknown group, or members remain and
                ``detach_members`` is False (the error lists them).
        """
        record = await self._resolve(group)
        members = await self._members(record.name)
        detached: list[str] = []
        if members:
            if not detach_members:
                raise SyncGroupError(
                    f"sync group {record.name!r} still has member(s) "
                    f"{members} — remove them first or pass "
                    f"detach_members=True"
                )
            async with self._membership_lock:
                detached = await self._repo.detach_members(record.name)
                for member in detached:
                    self._sync_runtime(member, None)
        existed = bool(await self._repo.delete(record.id))
        self._cache.pop(record.name, None)
        if existed:
            self._logger.info("sync group %s deleted (detached=%s)", record.name, detached)
        return {
            "deleted": record.name,
            "id": record.id,
            "existed": existed,
            "detached": detached,
        }

    # ----------------------------------------------------------- membership
    async def add_account_to_group(self, group: GroupRef, account: str) -> dict[str, Any]:
        """Put an account in a group (replaces any previous membership:
        None -> Alpha and Alpha -> Beta are both this one call).

        Raises:
            SyncGroupError: unknown group or unknown account.
        """
        record = await self._resolve(group)
        await self._require_account(account)
        async with self._membership_lock:
            await self._repo.set_account_group(account, record.name)
            self._sync_runtime(account, record.name)
        self._logger.info("account %s joined sync group %s", account, record.name)
        return {"account": account, "group": record.name}

    async def remove_account_from_group(self, group: GroupRef, account: str) -> dict[str, Any]:
        """Remove an account from ONE group (group -> None).

        Idempotent-lenient: removing an
        account that is not a member of THAT group reports
        ``existed: False`` instead of failing (its actual membership, if
        any, is untouched).

        Raises:
            SyncGroupError: unknown group or unknown account.
        """
        record = await self._resolve(group)
        await self._require_account(account)
        current = await self._repo.account_group_name(account)
        existed = current == record.name
        if existed:
            async with self._membership_lock:
                await self._repo.set_account_group(account, None)
                self._sync_runtime(account, None)
            self._logger.info("account %s left sync group %s", account, record.name)
        return {"account": account, "group": record.name, "existed": existed}

    async def set_account_group(self, account: str, group: GroupRef | None) -> dict[str, Any]:
        """The membership transition primitive: None -> G, G1 -> G2,
        G -> None and G -> G (idempotent) are all this one call.

        Raises:
            SyncGroupError: unknown account, or unknown group (when given).
        """
        await self._require_account(account)
        previous = await self._repo.account_group_name(account)
        if group is None:
            async with self._membership_lock:
                await self._repo.set_account_group(account, None)
                self._sync_runtime(account, None)
            name = None
        else:
            record = await self._resolve(group)
            async with self._membership_lock:
                await self._repo.set_account_group(account, record.name)
                self._sync_runtime(account, record.name)
            name = record.name
        return {"account": account, "group": name, "previous": previous}

    async def clear_account_group(self, account: str) -> dict[str, Any]:
        """Remove the account from any group (back to per-account pacing).

        Raises:
            SyncGroupError: unknown account.
        """
        return await self.set_account_group(account, None)

    async def get_group_members(self, group: GroupRef) -> list[str]:
        """Sorted account names of the group's members ([] when unknown)."""
        record = await self._resolve(group, required=False)
        if record is None:
            return []
        return await self._members(record.name)

    async def get_account_group(self, account: str) -> str | None:
        """The account's group NAME (None when in no group / unknown)."""
        if self._accounts is not None:
            try:
                return await self._repo.account_group_name(account)
            except Exception:
                self._logger.exception("could not read sync group of account %r", account)
        return None

    # ------------------------------------------------- framework plumbing
    async def sync_membership(self, account: str, group: str | None) -> None:
        """Adopt a membership written by a LEGACY path and sync the runtime.

        Used by AccountManager for ``add_account(sync_group=...)`` and
        config-dir payloads: when no group row exists for the name yet, one
        is created VERBATIM (no name validation — mirroring migration v5,
        legacy values must keep working), then the limiter mapping is set.
        No-op when ``group`` is empty.
        """
        if not group:
            return
        async with self._membership_lock:
            try:
                record = await self._repo.get_by_name(group)
            except Exception:
                self._logger.exception("could not look up sync group %r", group)
                record = self._cache.get(group)
            if record is None:
                try:
                    record = await self._repo.create(group)
                    self._cache[record.name] = record
                    self._logger.info("adopted legacy sync group %r (created on first use)", group)
                except Exception as exc:
                    self._logger.warning("could not adopt sync group %r: %s", group, exc)
                    return
            self._sync_runtime(account, group)

    async def forget_account(self, account: str) -> None:
        """Removal cascade: clear the account's runtime group mapping.

        No DB write — the membership pointer dies with the account row
        (AccountManager deletes it). The limiter cleanup here plus its own
        ``prune_account`` leave no stale ``group:<x>`` state behind.
        """
        async with self._membership_lock:
            self._sync_runtime(account, None)

    # -------------------------------------------------------------- snapshot
    async def snapshot(self, group: GroupRef) -> dict[str, Any]:
        """Application-level group snapshot (JSON-safe).

        Persistence fields come from the repository; ``pacing_key`` and
        ``runtime_members`` are DERIVED from the live limiter mapping (no
        duplicate runtime registry — divergence between the two member
        lists is diagnostic information).
        """
        record = await self._resolve(group)
        members = await self._members(record.name)
        runtime: dict[str, str] = {}
        mapping = getattr(self._limiter, "account_groups", None)
        if callable(mapping):
            try:
                runtime = mapping() or {}
            except Exception:
                runtime = {}
        return {
            **record.to_dict(),
            "member_count": len(members),
            "members": members,
            "pacing_key": f"group:{record.name}",
            "runtime_members": sorted(name for name, grp in runtime.items() if grp == record.name),
        }

    # -------------------------------------------------------------- helpers
    def _sync_runtime(self, account: str, group: str | None) -> None:
        """Feed one membership to the limiter (tolerant of unwired/limited
        doubles — pure metadata plumbing, no API involved)."""
        register = getattr(self._limiter, "set_account_group", None)
        if register is None:
            return
        try:
            register(account, group)
        except Exception:
            self._logger.exception("runtime group sync failed for account %r", account)

    def _validated_name(self, name: Any) -> str:
        """Strip + regex-validate a NEW group name (create/rename only —
        legacy adopted names bypass this by design)."""
        if not isinstance(name, str) or not NAME_RE.fullmatch(name.strip() or ""):
            raise SyncGroupError(
                f"sync group name {name!r} must match {NAME_RE.pattern} "
                "(lowercase letters/digits/_/-, at most 32 chars)"
            )
        return name.strip()

    @overload
    async def _resolve(
        self, group: GroupRef, *, required: Literal[True] = ...
    ) -> SyncGroupRecord: ...

    @overload
    async def _resolve(
        self, group: GroupRef, *, required: Literal[False]
    ) -> SyncGroupRecord | None: ...

    async def _resolve(self, group: GroupRef, *, required: bool = True) -> SyncGroupRecord | None:
        """GroupRef (int id | str name) -> SyncGroupRecord. With
        ``required=True`` (the default) an unresolved reference raises
        instead of returning None."""
        record: SyncGroupRecord | None = None
        try:
            if isinstance(group, int) and not isinstance(group, bool):
                record = await self._repo.get(group)
            elif isinstance(group, str):
                record = await self._repo.get_by_name(group.strip())
        except Exception:
            self._logger.exception("could not look up sync group %r", group)
            record = None
        if record is None:
            record = self._cache.get(group) if isinstance(group, str) else None
        if record is None and required:
            known = sorted(self._cache)
            raise SyncGroupError(f"unknown sync group {group!r} (known groups: {known})")
        return record

    async def _repo_get_by_name(self, name: str) -> SyncGroupRecord | None:
        record: SyncGroupRecord | None
        try:
            record = await self._repo.get_by_name(name)
        except Exception:
            record = self._cache.get(name)
        else:
            if record is not None:
                self._cache[record.name] = record
        return record

    async def _members(self, group_name: str) -> list[str]:
        try:
            return await self._repo.members(group_name)
        except Exception:
            self._logger.exception("could not read members of sync group %r", group_name)
            return []

    async def _membership_counts(self) -> dict[str, int]:
        try:
            return await self._repo.membership_counts() or {}
        except Exception:
            return {}

    async def _require_account(self, account: str) -> None:
        """Typo trap: the account must exist (when an accounts repo is
        wired) — with the known names in the error."""
        if self._accounts is None:
            return
        try:
            known = sorted(r.name for r in (await self._accounts.list() or []))
        except Exception:
            known = []
        if account not in known:
            raise SyncGroupError(f"unknown account {account!r} (known accounts: {known})")
