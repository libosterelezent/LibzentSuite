"""Credential handling: every token and user/bot ID lives in .env, never in code.

Design rules enforced by this module:
  * values are read from a .env file (or an injected dict for tests/simulations),
  * nothing that could identify a real account is hardcoded anywhere else,
  * repr/redacted output never leaks token values (dashboard/CLI safe display),
  * per spec §7: secrets are NOT considered safe merely because a DB exists —
    token values never enter the database at all; only .env key references do.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Mapping, Optional

LOG = logging.getLogger("slaughtermudae.credentials")

#: keys whose values must be masked in any human-facing output
SENSITIVE_KEYS = ("token", "secret", "password")
#: prefix that marks an env entry as an account token
TOKEN_PREFIX = "DISCORD_TOKEN_"


class CredentialsError(RuntimeError):
    """Raised when a required credential is missing or malformed."""


class Credentials:
    """Immutable-ish view over a parsed .env payload."""

    def __init__(self, values: Mapping[str, str], path: Optional[Path] = None) -> None:
        self._values: Dict[str, str] = {k: v for k, v in values.items() if v is not None}
        self._path = path

    # ------------------------------------------------------------------ load
    @staticmethod
    def parse_env(text: str) -> Dict[str, str]:
        """Parse .env text: comments, blank lines, optional 'export ', quotes."""
        out: Dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                out[key] = value
        return out

    @classmethod
    def from_file(cls, path: str | Path = ".env") -> "Credentials":
        """Load credentials from a .env file; a missing file yields an empty set."""
        p = Path(path)
        if not p.is_file():
            LOG.warning("credentials file %s not found; using empty credentials", p)
            return cls({}, p)
        return cls(cls.parse_env(p.read_text(encoding="utf-8")), p)

    @classmethod
    def from_values(cls, values: Mapping[str, str]) -> "Credentials":
        """Build credentials directly (tests/simulations with synthetic ids)."""
        return cls(values)

    # ----------------------------------------------------------------- access
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
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

    def token_for(self, account_name: str) -> Optional[str]:
        """MIGRATION-ONLY: reads DISCORD_TOKEN_<NAME> from .env.

        Used exclusively by the explicit `credentials import-env`
        migration command. Runtime credential resolution NEVER calls
        this — the encrypted database is the sole source.
        """
        return self._values.get(f"{TOKEN_PREFIX}{account_name.upper()}")

    # ------------------------------------------------------------- aggregates
    @property
    def mudae_bot_id(self) -> int:
        """Mudae bot user id (required int; plugins read it, never hardcode it)."""
        raw = self.require("MUDAE_BOT_ID")
        try:
            return int(raw)
        except ValueError as exc:
            raise CredentialsError(f"MUDAE_BOT_ID is not an integer: {raw!r}") from exc

    @property
    def owner_user_ids(self) -> frozenset[int]:
        """Comma-separated OWNER_USER_IDS -> frozenset of ints (empty if unset)."""
        raw = self._values.get("OWNER_USER_IDS", "") or ""
        ids = set()
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError as exc:
                raise CredentialsError(
                    f"OWNER_USER_IDS contains a non-integer: {part!r}"
                ) from exc
        return frozenset(ids)

    @property
    def dashboard_token(self) -> Optional[str]:
        """Optional local auth token for the dashboard."""
        return self._values.get("DASHBOARD_TOKEN") or None

    @property
    def remote_control_enabled(self) -> bool:
        """Mention-prefix management from Discord (REMOTE_CONTROL=0 opts out)."""
        raw = (self._values.get("REMOTE_CONTROL") or "1").strip().lower()
        return raw not in ("0", "false", "no", "off")

    # ---------------------------------------------------------------- display
    def redacted(self) -> Dict[str, str]:
        """Human-safe copy: token-like values masked, never fully shown."""
        safe: Dict[str, str] = {}
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

    def source_path(self) -> Optional[Path]:
        """File the values came from, if any."""
        return self._path


__all__ = ["Credentials", "CredentialsError"]
