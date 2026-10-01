"""Profile manager (spec §4): named capability bundles assigned to accounts.

A profile names a set of plugins an account MAY run. Accounts can hold
several profiles; the effective grant is the UNION of their plugin lists.
An account with NO known profile is UNRESTRICTED (backwards compatible with
pre-profile configurations) — this mirrors the audited ``core/roles.py``
semantics exactly (``allowed_plugins`` returning ``None``).

Persistence is read-through: the ``ProfilesRepository`` (and the optional
``AccountsRepository`` for assignments) is the single source of truth; this
manager keeps no duplicate cache, so CLI/dashboard edits through any path are
immediately visible here.

Ported from ``git show HEAD:core/roles.py`` (name regex, dedupe+sort plugin
lists, unknown-role warning behaviour) with the new repository API names.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set

from ..errors import ProfileError

#: profile names: lowercase identifier, may start with a digit (unlike the
#: old role regex — profiles and accounts share one namespace style here)
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class ProfileManager:
    """CRUD + assignment for profiles; validates at every boundary.

    All mutating methods persist FIRST through the repository and only then
    report success; read methods go straight to the repository.
    """

    NAME_RE = NAME_RE

    def __init__(self, profiles_repo: Any, logger: Any = None,
                 accounts_repo: Any = None) -> None:
        """``profiles_repo``: ProfilesRepository; ``accounts_repo`` (optional)
        enables assignment validation + lookups; ``logger``: stdlib-style.
        """
        self._profiles = profiles_repo
        self._accounts = accounts_repo
        self._logger = (logger or logging.getLogger("slaughtermudae.profiles"))

    # ---------------------------------------------------------------- CRUD
    def set_profile(self, name: str, plugins: Any, description: str = "") -> Dict[str, Any]:
        """Create or fully replace a profile (persisted via repo.upsert).

        The plugin list is stripped, de-duplicated and sorted (canonical form
        on disk — ported from the audited roles code).

        Raises:
            ProfileError: bad name (must match ``^[a-z][a-z0-9_-]{0,31}$``),
                a non-list plugin argument, or an empty/dirty plugin list.
        """
        if not isinstance(name, str) or not NAME_RE.match(name or ""):
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
        self._profiles.upsert(name, cleaned, description)
        self._logger.info("profile %s set (plugins=%s)", name, cleaned)
        return {"name": name, "description": description, "plugins": cleaned}

    def delete_profile(self, name: str) -> Dict[str, Any]:
        """Delete a profile (repo cascades account assignments).

        Returns ``{"deleted": name, "existed": bool}`` — deleting an unknown
        profile is NOT an error (idempotent).
        """
        existed = bool(self._profiles.delete(name))
        if existed:
            self._logger.info("profile %s deleted", name)
        else:
            self._logger.info("profile %r did not exist (delete no-op)", name)
        return {"deleted": name, "existed": existed}

    def profiles(self) -> List[Dict[str, Any]]:
        """Every profile as ``[{"name","description","plugins"}]``, sorted."""
        records = self._profiles.list() or []
        return [
            {
                "name": r.name,
                "description": r.description,
                "plugins": list(r.plugins),
            }
            for r in sorted(records, key=lambda r: r.name)
        ]

    # ---------------------------------------------------------- assignment
    def assign(self, account: str, profile: str) -> Dict[str, Any]:
        """Attach a profile to an account (persisted via accounts repo).

        Validates BOTH sides first: the profile must exist (otherwise
        ``ProfileError`` listing the known profiles) and, when an accounts
        repository was given, the account must exist too (otherwise
        ``ProfileError`` listing the known accounts).

        Assignments take effect on the account's NEXT start.
        """
        known = self._known_profile_names()
        if profile not in known:
            raise ProfileError(
                f"unknown profile {profile!r} (known profiles: {known})"
            )
        if self._accounts is not None and self._accounts.get(account) is None:
            raise ProfileError(
                f"unknown account {account!r} "
                f"(known accounts: {self._known_account_names()})"
            )
        if self._accounts is not None:
            self._accounts.assign_profile(account, profile)
        self._logger.info("profile %r assigned to account %r", profile, account)
        return {
            "account": account,
            "profile": profile,
            "profiles": self.assigned(account),
        }

    def unassign(self, account: str, profile: str) -> Dict[str, Any]:
        """Detach a profile from an account (idempotent; persisted).

        The PROFILE's existence is deliberately not required — a profile
        deleted from the repository cascades its assignments anyway, so a
        stale detach must never hard-fail. The ACCOUNT is validated when an
        accounts repository is available (typo trap).
        """
        if self._accounts is not None and self._accounts.get(account) is None:
            raise ProfileError(
                f"unknown account {account!r} "
                f"(known accounts: {self._known_account_names()})"
            )
        existed = profile in self.assigned(account)
        if self._accounts is not None:
            self._accounts.unassign_profile(account, profile)
        self._logger.info("profile %r unassigned from account %r (was assigned: %s)",
                          profile, account, existed)
        return {
            "account": account,
            "profile": profile,
            "existed": existed,
            "profiles": self.assigned(account),
        }

    def assigned(self, account: str) -> List[str]:
        """Sorted names of the profiles assigned to ``account`` ([] if none)."""
        if self._accounts is None:
            return []
        return sorted(self._accounts.profiles_of(account) or [])

    def allowed_plugins(self, account: str) -> Optional[Set[str]]:
        """UNION of plugin grants across the account's KNOWN profiles.

        Returns ``None`` when the account has no KNOWN profile — meaning
        "no restriction" (backwards-compatible default, ported from the
        audited roles code). Unknown profile names are logged and skipped:
        a typo must never widen or silently narrow the grant.
        """
        allowed: Set[str] = set()
        seen_known = False
        for name in self.assigned(account):
            record = self._profiles.get(name)
            if record is None:
                self._logger.warning(
                    "account %r references unknown profile %r (ignored)",
                    account, name,
                )
                continue
            seen_known = True
            allowed.update(record.plugins)
        return allowed if seen_known else None

    # -------------------------------------------------------------- helpers
    def _known_profile_names(self) -> List[str]:
        """Sorted known profile names (for human error messages)."""
        return sorted(r.name for r in (self._profiles.list() or []))

    def _known_account_names(self) -> List[str]:
        """Sorted known account names (for human error messages)."""
        if self._accounts is None:
            return []
        try:
            return sorted(r.name for r in (self._accounts.list() or []))
        except Exception:  # repository shape drift — never mask the real error
            return []


__all__ = ["ProfileManager", "NAME_RE"]
