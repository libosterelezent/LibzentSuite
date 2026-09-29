"""Credential handling: every token and user/bot ID lives in .env, never in code.

Design rules enforced by this module:
  * Values are read from a .env file (or injected dict for tests/simulations).
  * Nothing that could identify a real account is hardcoded anywhere else.
  * repr/redacted output never leaks token values (dashboard/CLI safe display).
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
        """Parse .env text into a dict.

        Supports: comments (#), blank lines, optional 'export ', single or double
        quoted values, KEY=VALUE pairs. Values keep internal newlines only when
        quoted.
        """
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
        """Load credentials from a .env file; missing file yields empty set."""
        p = Path(path)
        if not p.is_file():
            LOG.warning("credentials file %s not found; using empty credentials", p)
            return cls({}, p)
        return cls(cls.parse_env(p.read_text(encoding="utf-8")), p)

    @classmethod
    def from_values(cls, values: Mapping[str, str]) -> "Credentials":
        """Build credentials directly (used by tests/simulations with synthetic ids)."""
        return cls(values)

    # ----------------------------------------------------------------- access
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Return raw string value or default."""
        return self._values.get(key, default)

    def require(self, key: str) -> str:
        """Return value or raise CredentialsError (for hard requirements)."""
        value = self._values.get(key)
        if not value:
            raise CredentialsError(
                f"missing required credential {key!r} (set it in {self._path or '.env'})"
            )
        return value

    # ------------------------------------------------------------- aggregates
    def account_tokens(self) -> Dict[str, str]:
        """Map account name (lowercased suffix) -> token for DISCORD_TOKEN_* keys."""
        return {
            key[len(TOKEN_PREFIX):].lower(): value
            for key, value in self._values.items()
            if key.startswith(TOKEN_PREFIX) and value
        }

    def token_for(self, account_name: str) -> Optional[str]:
        """Token lookup for an account using the DISCORD_TOKEN_<NAME> convention."""
        return self._values.get(f"{TOKEN_PREFIX}{account_name.upper()}")

    @property
    def mudae_bot_id(self) -> int:
        """Mudae bot user id (required int)."""
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
                raise CredentialsError(f"OWNER_USER_IDS contains a non-integer: {part!r}") from exc
        return frozenset(ids)

    @property
    def dashboard_token(self) -> Optional[str]:
        """Optional local auth token for the GUI dashboard."""
        return self._values.get("DASHBOARD_TOKEN") or None

    # ---------------------------------------------------------------- display
    def redacted(self) -> Dict[str, str]:
        """Human-safe copy: token-like values masked to first 4 chars + length."""
        safe: Dict[str, str] = {}
        for key, value in self._values.items():
            if not value:
                safe[key] = ""
            elif "token" in key.lower() or any(s in key.lower() for s in SENSITIVE_KEYS):
                safe[key] = f"{value[:4]}…({len(value)} chars)"
            else:
                safe[key] = value
        return safe

    def __repr__(self) -> str:  # safety net for accidental log of secrets
        return f"<Credentials path={self._path!r} keys={sorted(self._values)} redacted>"

    def source_path(self) -> Optional[Path]:
        """File the values came from, if any."""
        return self._path
