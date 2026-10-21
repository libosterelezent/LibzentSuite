"""Profile manager (spec §4): named capability bundles assigned to accounts.

A profile names a set of plugins an account MAY run. Accounts can hold
several profiles; the effective grant is the UNION of their plugin lists.
An account with NO known profile is UNRESTRICTED (backwards compatible with
pre-profile configurations) — this mirrors the audited ``core/roles.py``
semantics exactly (``allowed_plugins`` returning ``None``).

Persistence is read-through into an in-memory cache: the repositories are
the source of truth, :meth:`load` (awaited once at boot, after
``database.connect()``) populates the cache and every mutating method
updates it right after the repository write succeeds. Mutating methods are
async (aiosqlite repos underneath); :meth:`allowed_plugins` stays SYNC and
answers from the cache because the account manager calls it from attach
paths that must not grow another await.

Ported from ``git show HEAD:core/roles.py`` (name regex, dedupe+sort plugin
lists, unknown-role warning behaviour) with the new repository API names.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from ..errors import ProfileError

#: profile names: a lowercase letter first, then lowercase letters/digits/
#: underscore/dash, at most 32 chars (leading digits rejected — pinned by
#: tests; only ACCOUNT names may start with a digit). fullmatch (NOT
#: ``...$`` anchoring): ``$`` also matches before a trailing newline, which
#: let names like ``"basic\n"`` through the old pattern.
from ..names import PROFILE_NAME_RE as NAME_RE


class ProfileManager:
    """CRUD + assignment for profiles; validates at every boundary.

    All mutating methods persist FIRST through the repository (awaited)
    and only then report success + update the in-memory cache; reads go
    through the cache kept fresh by :meth:`load` and the mutators.
    """

    NAME_RE = NAME_RE

    def __init__(self, profiles_repo: Any, logger: Any = None,
                 accounts_repo: Any = None) -> None:
        """``profiles_repo``: ProfilesRepository; ``accounts_repo`` (optional)
        enables assignment validation + lookups; ``logger``: stdlib-style.
        The constructor does NO I/O — await :meth:`load` at boot.
        """
        self._profiles = profiles_repo
        self._accounts = accounts_repo
        self._logger = (logger or logging.getLogger("libzent.profiles"))
        #: name -> ProfileRecord (cache; refresh via load()/set_profile)
        self._profile_cache: Dict[str, Any] = {}
        #: account -> sorted profile names (cache; refresh via load()/assign)
        self._assignment_cache: Dict[str, List[str]] = {}

    def forget_account(self, account: str) -> None:
        """Drop an account's assignment-cache entry (removal cascade).

        K14 (S6-C2): a cached assignment for a removed account would be
        silently re-inherited (profile grants!) by a re-created
        same-named account in the same process.
        """
        self._assignment_cache.pop(account, None)

    # ---------------------------------------------------------------- boot
    async def load(self) -> None:
        """Populate the caches from the repositories (boot path).

        Best-effort per repository: a failure is logged and leaves that
        cache empty — profile checks then degrade to "unrestricted" rather
        than failing the boot.
        """
        try:
            for record in (await self._profiles.list() or []):
                self._profile_cache[record.name] = record
        except Exception:
            self._logger.exception("could not load profiles from repository")
        if self._accounts is not None:
            try:
                for record in (await self._accounts.list() or []):
                    self._assignment_cache[record.name] = sorted(record.profiles or [])
            except Exception:
                self._logger.exception("could not load profile assignments")

    # ---------------------------------------------------------------- CRUD
    async def set_profile(self, name: str, plugins: Any,
                          description: str = "") -> Dict[str, Any]:
        """Create or fully replace a profile (persisted via repo.upsert).

        The plugin list is stripped, de-duplicated and sorted (canonical form
        on disk — ported from the audited roles code).

        Raises:
            ProfileError: bad name (must match ``^[a-z][a-z0-9_-]{0,31}``),
                a non-list plugin argument, or an empty/dirty plugin list.
        """
        if not isinstance(name, str) or not NAME_RE.fullmatch(name or ""):
            raise ProfileError(
                f"profile name {name!r} must match {NAME_RE.pattern} "
                "(lowercase letters/digits/_/-, at most 32 chars)"
            )
        if isinstance(plugins, (str, bytes)) or not hasattr(plugins, "__iter__"):
            raise ProfileError(
                f"profile {name!r} plugins must be a list of plugin names, "
                f"got {type(plugins).__name__}"
            )
        if not isinstance(description, str):
            raise ProfileError(
                f"profile {name!r} description must be a string, "
                f"got {type(description).__name__}"
            )
        cleaned: List[str] = sorted({str(p).strip() for p in plugins if str(p).strip()})
        if not cleaned:
            raise ProfileError(
                f"profile {name!r} must allow at least one plugin "
                "(got an empty list)"
            )
        await self._profiles.upsert(name, cleaned, description)
        # cache the CANONICAL locally computed record: the old re-read
        # was best-effort inside except-pass, so a failed re-read left
        # the STALE previous definition in the cache while the
        # repository already held the new one
        from database.models import ProfileRecord

        self._profile_cache[name] = ProfileRecord(
            name=name, plugins=list(cleaned), description=description)
        self._logger.info("profile %s set (plugins=%s)", name, cleaned)
        return {"name": name, "description": description, "plugins": cleaned}

    async def delete_profile(self, name: str) -> Dict[str, Any]:
        """Delete a profile (repo cascades account assignments).

        Returns ``{"deleted": name, "existed": bool}`` — deleting an unknown
        profile is NOT an error (idempotent).
        """
        existed = bool(await self._profiles.delete(name))
        self._profile_cache.pop(name, None)
        for assigned in self._assignment_cache.values():
            if name in assigned:
                assigned.remove(name)
        if existed:
            self._logger.info("profile %s deleted", name)
        else:
            self._logger.info("profile %r did not exist (delete no-op)", name)
        return {"deleted": name, "existed": existed}

    async def profiles(self) -> List[Dict[str, Any]]:
        """Every profile as ``[{"name","description","plugins"}]``, sorted.

        Read-through: refreshes the cache from the repository so edits made
        through any path are visible here.
        """
        try:
            records = await self._profiles.list() or []
        except Exception:
            self._logger.exception("could not list profiles")
            records = list(self._profile_cache.values())
        for record in records:
            self._profile_cache[record.name] = record
        return [
            {
                "name": r.name,
                "description": r.description,
                "plugins": list(r.plugins),
            }
            for r in sorted(records, key=lambda r: r.name)
        ]

    # ---------------------------------------------------------- assignment
    async def assign(self, account: str, profile: str) -> Dict[str, Any]:
        """Attach a profile to an account (persisted via accounts repo).

        Validates BOTH sides first: the profile must exist (otherwise
        ``ProfileError`` listing the known profiles) and, when an accounts
        repository was given, the account must exist too (otherwise
        ``ProfileError`` listing the known accounts).

        Assignments take effect on the account's NEXT start.
        """
        known = await self._known_profile_names()
        if profile not in known:
            raise ProfileError(
                f"unknown profile {profile!r} (known profiles: {known})"
            )
        if self._accounts is not None and await self._accounts.get(account) is None:
            raise ProfileError(
                f"unknown account {account!r} "
                f"(known accounts: {await self._known_account_names()})"
            )
        if self._accounts is not None:
            await self._accounts.assign_profile(account, profile)
        assigned = await self.assigned(account)
        self._logger.info("profile %r assigned to account %r", profile, account)
        return {
            "account": account,
            "profile": profile,
            "profiles": assigned,
        }

    async def unassign(self, account: str, profile: str) -> Dict[str, Any]:
        """Detach a profile from an account (idempotent; persisted).

        The PROFILE's existence is deliberately not required — a profile
        deleted from the repository cascades its assignments anyway, so a
        stale detach must never hard-fail. The ACCOUNT is validated when an
        accounts repository is available (typo trap).
        """
        if self._accounts is not None and await self._accounts.get(account) is None:
            raise ProfileError(
                f"unknown account {account!r} "
                f"(known accounts: {await self._known_account_names()})"
            )
        existed = profile in await self.assigned(account)
        if self._accounts is not None:
            await self._accounts.unassign_profile(account, profile)
        assigned = await self.assigned(account)
        self._logger.info("profile %r unassigned from account %r (was assigned: %s)",
                          profile, account, existed)
        return {
            "account": account,
            "profile": profile,
            "existed": existed,
            "profiles": assigned,
        }

    async def assigned(self, account: str) -> List[str]:
        """Sorted names of the profiles assigned to ``account`` ([] if none).

        Read-through into the cache (assignment cache stays fresh for the
        sync :meth:`allowed_plugins`).
        """
        if self._accounts is None:
            return []
        try:
            names = sorted(await self._accounts.profiles_of(account) or [])
        except Exception:
            self._logger.exception("could not read profiles of %r", account)
            return list(self._assignment_cache.get(account, []))
        self._assignment_cache[account] = names
        return names

    def allowed_plugins(self, account: str) -> Optional[Set[str]]:
        """UNION of plugin grants across the account's KNOWN profiles.

        Returns ``None`` when the account has NO profile assigned at
        all — meaning "no restriction" (backwards-compatible default,
        ported from the audited roles code). When the account DOES have
        assignments but they are ALL unknown profile names (typo'd or
        deleted out-of-band), an EMPTY set is returned instead: the
        grant must never widen to "unrestricted" just because every
        name failed to resolve, and the condition is warned about
        loudly. Mixed known/unknown assignments keep the union of the
        known ones (an unknown name is logged and skipped).

        SYNC by design: answers from the in-memory cache populated by
        :meth:`load` and kept fresh by assign/unassign/delete (the account
        manager calls this from its attach path).
        """
        assigned = self._assignment_cache.get(account, [])
        if not assigned:
            return None  # no assignments at all: unrestricted (legacy)
        allowed: Set[str] = set()
        unknown: List[str] = []
        for name in assigned:
            record = self._profile_cache.get(name)
            if record is None:
                unknown.append(name)
                continue
            allowed.update(record.plugins)
        if unknown:
            if not allowed:
                self._logger.warning(
                    "account %r has %d profile assignment(s) but NONE of "
                    "them is a known profile (%s) — allowing NOTHING "
                    "(empty grant); fix or remove the dangling "
                    "assignments",
                    account, len(assigned), ", ".join(map(repr, unknown)),
                )
                return set()
            self._logger.warning(
                "account %r references unknown profile(s) %s (ignored; "
                "the known profiles' union still applies)",
                account, ", ".join(map(repr, unknown)),
            )
        return allowed

    # -------------------------------------------------------------- helpers
    async def _known_profile_names(self) -> List[str]:
        """Sorted known profile names (for human error messages)."""
        try:
            records = await self._profiles.list() or []
        except Exception:  # repository shape drift — never mask the real error
            records = list(self._profile_cache.values())
        for record in records:
            self._profile_cache[record.name] = record
        return sorted(r.name for r in records)

    async def _known_account_names(self) -> List[str]:
        """Sorted known account names (for human error messages)."""
        if self._accounts is None:
            return []
        try:
            return sorted(r.name for r in (await self._accounts.list() or []))
        except Exception:  # repository shape drift — never mask the real error
            return sorted(self._assignment_cache)


__all__ = ["ProfileManager", "NAME_RE"]
