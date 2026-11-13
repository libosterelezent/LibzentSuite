"""Credential handling: every token and user/bot ID lives in .env, never in code.

Design rules enforced by this module:
  * values are read from a .env file (or an injected dict for tests/simulations),
  * nothing that could identify a real account is hardcoded anywhere else,
  * repr/redacted output never leaks token values (dashboard/CLI safe display),
  * per spec §7: account tokens are NOT stored here at runtime — the
    encrypted database store (core.credential_service.CredentialService) is
    the sole source; this module keeps fleet-level ids/flags and the
    DISCORD_TOKEN_* keys are read only by the one-shot `import-env`
    migration.

ADMIN-PANEL MIGRATION (this file's second job): the authorization model
moved OFF the env and INTO the database. :class:`AccessManager` is the
DB-backed store for

  * the framework ADMIN user-id list (the replacement for the global
    ``OWNER_USER_IDS`` grant — see the security note on
    :meth:`AccessManager.migrate_from_credentials`),
  * per-account COMMAND PERMISSIONS (the Discord user ids allowed to
    invoke that one account's self-commands),
  * per-account ADMIN flags and PREFIX configurations,
  * the framework-level ``PRIMARY_BOT_ID`` setting (DB value with the env
    value as a read-only fallback),

and :class:`FrameworkCredentials` is the DB-first view handed to plugin
services as ``services.credentials`` (plugins keep reading
``.owner_user_ids`` / ``.primary_bot_id``; the values now come from the
admin-managed store, falling back to env when the DB value is unset).
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import CredentialsError  # ONE class (was defined twice)

if TYPE_CHECKING:
    from collections.abc import Mapping

LOG = logging.getLogger("libzent.credentials")

#: keys whose values must be masked in any human-facing output
SENSITIVE_KEYS = ("token", "secret", "password")
#: prefix that marks an env entry as an account token
TOKEN_PREFIX = "DISCORD_TOKEN_"

#: guards the one-time unrecognized-REMOTE_CONTROL warning (module logger,
#: once per process — the gate itself stays non-raising on every read)
_remote_control_warned = False


class Credentials:
    """Immutable-ish view over a parsed .env payload."""

    def __init__(self, values: Mapping[str, str], path: Path | None = None) -> None:
        self._values: dict[str, str] = {k: v for k, v in values.items() if v is not None}
        self._path = path
        self._owner_ids: frozenset | None = None  # parsed once, lazily

    # ------------------------------------------------------------------ load
    @staticmethod
    def parse_env(text: str) -> dict[str, str]:
        """Parse .env text: comments, blank lines, optional 'export ', quotes."""
        out: dict[str, str] = {}
        text = text.lstrip("\ufeff")  # UTF-8 BOM (Windows editors) hides line 1's key
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # cut inline comments on the UNSTRIPPED value: strip() first
            # would eat the very space that marks ' #' as a comment, so
            # 'KEY= # disabled' parsed as '# disabled' instead of ''
            cut = -1
            if value.lstrip()[:1] not in ('"', "'"):
                # unquoted value: ' # comment' ends it (a '#' with no space
                # before it is part of the value, e.g. tokens/passwords)
                cut = value.find(" #")
                if cut == -1:
                    cut = value.find("\t#")
            if cut != -1:
                value = value[:cut].rstrip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                out[key] = value
        return out

    @classmethod
    def from_file(cls, path: str | Path = ".env") -> Credentials:
        """Load credentials from a .env file; a missing file yields an empty set."""
        p = Path(path)
        if not p.is_file():
            LOG.warning("credentials file %s not found; using empty credentials", p)
            return cls({}, p)
        try:
            # lstat does NOT follow the link: a symlinked .env (a classic
            # exfil pivot — point the framework at an attacker-chosen
            # file) is rejected BEFORE any read happens
            if stat.S_ISLNK(os.lstat(p).st_mode):
                raise CredentialsError(
                    f"credentials file {p} is a symlink — refusing to "
                    "follow it (remove the link or use a real file)"
                )
        except OSError as exc:
            raise CredentialsError(f"cannot read credentials file {p}: {exc}") from exc
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CredentialsError(f"cannot read credentials file {p}: {exc}") from exc
        return cls(cls.parse_env(text), p)

    @classmethod
    def from_values(cls, values: Mapping[str, str]) -> Credentials:
        """Build credentials directly (tests/simulations with synthetic ids)."""
        return cls(values)

    # ----------------------------------------------------------------- access
    def snapshot(self) -> dict[str, str]:
        """Copy of all parsed values (overlaying consumers never mutate us)."""
        return dict(self._values)

    def get(self, key: str, default: str | None = None) -> str | None:
        """Raw string value or default."""
        return self._values.get(key, default)

    def require(self, key: str) -> str:
        """Value or raise CredentialsError (for hard requirements)."""
        value = self._values.get(key)
        if not value:
            raise CredentialsError(
                f"missing required credential {key!r} (set it in {self._path or '.env'})"
            )
        return value

    def token_for(self, account_name: str) -> str | None:
        """MIGRATION-ONLY: reads DISCORD_TOKEN_<NAME> from .env.

        Used exclusively by the explicit `credentials import-env`
        migration command. Runtime credential resolution NEVER calls
        this — the encrypted database is the sole source.
        """
        return self._values.get(f"{TOKEN_PREFIX}{account_name.upper()}")

    # ------------------------------------------------------------- aggregates
    @property
    def primary_bot_id(self) -> int:
        """the primary bot bot user id (env value — LEGACY fallback).

        The authoritative source is the admin-panel setting served by
        :class:`AccessManager.primary_bot_id` (DB first, this env value as
        the fallback when the DB value is unset). Plugins never read this
        property directly — they see the DB-first view through
        ``services.credentials`` (:class:`FrameworkCredentials`).
        """
        raw = self.require("PRIMARY_BOT_ID")
        try:
            return int(raw)
        except ValueError as exc:
            # truncated repr: the raw value can be arbitrarily long and
            # must not be echoed wholesale into logs/errors
            shown = raw if len(raw) <= 40 else raw[:37] + "..."
            raise CredentialsError(f"PRIMARY_BOT_ID is not an integer: {shown!r}") from exc

    @property
    def owner_user_ids(self) -> frozenset[int]:
        """Comma-separated OWNER_USER_IDS -> frozenset of ints (empty if unset).

        DEPRECATED for authorization: this list is read ONCE at boot by
        :meth:`AccessManager.migrate_from_credentials` to seed the DB
        admin list (first boot only) and is then IGNORED — command
        authorization lives in the per-account permission lists + the
        admin list. It remains the `Credentials` object's parsed view for
        diagnostics and legacy unwired stacks.

        Parsed once and cached (this is read on EVERY inbound message by
        the remote-control gate; the values are immutable after load). A
        malformed value is cached as an EMPTY set: fail closed, cheap and
        non-raising on every access — the boot path warns once and the
        gate then quietly rejects every mention (never a per-message
        raise, never a partially-armed owner set).
        """
        if self._owner_ids is not None:
            return self._owner_ids
        raw = self._values.get("OWNER_USER_IDS", "") or ""
        ids = set()
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                LOG.warning(
                    "OWNER_USER_IDS contains a non-integer: %r — remote "
                    "control will reject every mention",
                    part,
                )
                self._owner_ids = frozenset()
                return self._owner_ids
        self._owner_ids = frozenset(ids)
        return self._owner_ids

    @property
    def dashboard_token(self) -> str | None:
        """Optional local auth token for the dashboard."""
        return self._values.get("DASHBOARD_TOKEN") or None

    @property
    def self_command_prefix(self) -> str:
        """Prefix that lets the framework account command ITSELF.

        SELF_COMMAND_PREFIX in .env: a string (e.g. ``sm!``) or ``@me``
        (the account's own mention). When set, each live client runs the
        library's ext.commands layer in self_bot mode — messages typed by
        the account itself (from the user's Discord client) that match
        the prefix become framework commands (registry dispatch to the
        plugin command handlers). Empty/unset keeps today's behavior
        exactly (no command layer).

        Requirement: the account's own Discord user id must also be in
        OWNER_USER_IDS for the triggers to pass the owner check.
        """
        return (self._values.get("SELF_COMMAND_PREFIX") or "").strip()

    @property
    def remote_control_enabled(self) -> bool:
        """Mention-prefix management from Discord (REMOTE_CONTROL=0 opts out).

        FAIL CLOSED: an absent key keeps the documented default (on);
        a present-but-empty or unrecognized value DISABLES the gate —
        the old parse failed OPEN (anything unrecognized meant enabled).
        """
        global _remote_control_warned
        raw = self._values.get("REMOTE_CONTROL")
        if raw is None:
            return True  # documented default: enabled
        value = raw.strip().lower()
        if value in ("1", "true", "yes", "on"):
            return True
        if value in ("0", "false", "no", "off", ""):
            return False
        if not _remote_control_warned:
            _remote_control_warned = True
            LOG.warning(
                "REMOTE_CONTROL=%r is not a recognized value (expected "
                "1/true/yes/on or 0/false/no/off) — remote control is "
                "DISABLED (fail closed)",
                raw[:40],
            )
        return False

    # ---------------------------------------------------------------- display
    def redacted(self) -> dict[str, str]:
        """Human-safe copy: token-like values masked, never fully shown."""
        safe: dict[str, str] = {}
        for key, value in self._values.items():
            if not value:
                safe[key] = ""
            elif "token" in key.lower() or any(s in key.lower() for s in SENSITIVE_KEYS):
                safe[key] = f"{value[:4]}…({len(value)} chars)" if len(value) > 4 else "***"
            else:
                safe[key] = value
        return safe

    def __repr__(self) -> str:  # safety net for accidental log of secrets
        return f"<Credentials path={self._path!r} keys={sorted(self._values)} redacted>"

    def source_path(self) -> Path | None:
        """File the values came from, if any."""
        return self._path


__all__ = ["Credentials", "CredentialsError"]
