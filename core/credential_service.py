"""CredentialService — encrypted credential storage (spec §12-§14).

Threat model (honest, spec §44):
* DB file stolen/copied       -> attacker sees Fernet CIPHERTEXT only; the
                                 key never lives in the database.
* Running process compromised -> plaintext credentials may exist in
                                 memory; encryption-at-rest does NOT
                                 protect against this.
* Dashboard compromised       -> severe by design (it manages accounts);
                                 credential routes therefore REQUIRE
                                 DASHBOARD_TOKEN auth, and the bind stays
                                 127.0.0.1 by default.

Key management (spec §12): ``SM_MASTER_KEY`` env (urlsafe base64 Fernet
key) when set; otherwise a generated key file at ``state/credentials.key``
(chmod 0600, outside the DB, gitignored via state/). Losing the key means
re-entering credentials — by design (no recoverable plaintext anywhere).

Crypto: ``cryptography.fernet.MultiFernet``-compatible single Fernet
(AES-128-CBC + HMAC-SHA256, established library — nothing homemade).

Precedence on credential resolution (spec §17):
    DB credential exists        -> use it
    DB absent, .env has token   -> legacy fallback (never overwrites DB)
    neither                     -> MISSING_CREDENTIAL
The .env import command is EXPLICIT and one-shot per account.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from .errors import CredentialsError

LOG = logging.getLogger("slaughtermudae.credentials")

KEY_ENV = "SM_MASTER_KEY"
DEFAULT_KEY_PATH = Path("state/credentials.key")


class CredentialService:
    """Encrypt-at-rest credential storage over the credentials repository."""

    def __init__(self, repository: Optional[Any] = None,
                 key_path: Path | str | None = None) -> None:
        self._repo = repository
        self._key_path = Path(key_path) if key_path is not None else DEFAULT_KEY_PATH
        self._fernet: Optional[Fernet] = None
        self._memory: dict = {}       # runtime cache: account -> plaintext (§14)
        self._runtime_sources: dict = {}  # account -> "db" | "env"
        self._db_accounts: set = set()  # accounts with a stored credential

    async def refresh(self) -> None:
        """Re-read the stored-credential account list (boot/status display)."""
        if self._repo is not None:
            self._db_accounts = set(await self._repo.accounts_with_credential())

    # ------------------------------------------------------------------ key
    def _load_or_create_key(self) -> bytes:
        """Master key: SM_MASTER_KEY env > key file (created on first run)."""
        env_key = os.environ.get(KEY_ENV, "").strip()
        if env_key:
            try:
                Fernet(env_key.encode())
                return env_key.encode()
            except Exception as exc:
                raise CredentialsError(
                    f"{KEY_ENV} is set but not a valid Fernet key "
                    "(generate one: python -c 'from cryptography.fernet import "
                    "Fernet; print(Fernet.generate_key().decode())')"
                ) from exc
        key = self._read_key_file()
        if key:
            return key
        key = Fernet.generate_key()
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # created 0600 ATOMICALLY (write_text + chmod left a window in
            # which the key was world-readable) and O_EXCL so two processes
            # booting together can never overwrite each other's key
            fd = os.open(str(self._key_path),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = self._read_key_file()
            if existing:
                return existing
            raise CredentialsError(
                f"credential key file {self._key_path} exists but is empty")
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        LOG.info("generated new credential master key at %s "
                 "(lose it and stored credentials must be re-entered)",
                 self._key_path)
        return key

    def _read_key_file(self) -> Optional[bytes]:
        """The stored key (stripped) or None when absent/blank."""
        if not self._key_path.is_file():
            return None
        try:
            text = self._key_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise CredentialsError(
                f"credential key file {self._key_path} is unreadable: {exc}"
            ) from exc
        return text.encode() if text else None

    @property
    def fernet(self) -> Fernet:
        """Lazily-initialized cipher (created on first use, not at boot)."""
        if self._fernet is None:
            key = self._load_or_create_key()
            try:
                self._fernet = Fernet(key)
            except Exception as exc:  # corrupt/truncated key file
                raise CredentialsError(
                    f"credential master key in {self._key_path} is not a "
                    "valid Fernet key (corrupt file? restore it, or delete "
                    "it and re-enter the stored credentials)") from exc
        return self._fernet

    # ------------------------------------------------------------ storage
    async def store(self, account: str, token: str) -> None:
        """Encrypt + persist a credential and cache it for runtime use.

        No network activity — saving is always separate from connecting
        (spec §16). Plaintext exists only in the runtime cache and the
        caller's argument; the repository receives ciphertext only.
        """
        token = (token or "").strip()
        if not token:
            raise CredentialsError("credential value must not be empty")
        ciphertext = self.fernet.encrypt(token.encode())
        if self._repo is not None:
            await self._repo.store(account, ciphertext)
            self._db_accounts.add(account)
        self._memory[account] = token
        self._runtime_sources[account] = "db"

    async def resolve(self, account: str) -> Optional[str]:
        """Runtime resolution — DATABASE ONLY (no .env fallback).

        The decrypted credential comes exclusively from the encrypted
        credentials table. No environment-variable account-token
        convention exists at runtime; .env tokens enter only through
        the EXPLICIT one-shot ``import_from_env`` migration command.
        """
        cached = self._memory.get(account)
        if cached is not None:
            return cached
        if self._repo is not None and account not in self._db_accounts:
            await self.refresh()  # unknown name: one list query keeps status() truthful
        if self._repo is not None:
            ciphertext = await self._repo.load(account)
            if ciphertext is not None:
                try:
                    token = self.fernet.decrypt(ciphertext).decode()
                except InvalidToken as exc:
                    raise CredentialsError(
                        f"stored credential for {account!r} cannot be "
                        "decrypted (master key changed? re-enter it)"
                    ) from exc
                self._memory[account] = token
                self._runtime_sources[account] = "db"
                return token
        return None

    async def delete(self, account: str) -> bool:
        """Remove the stored credential + cached plaintext.

        Memory caches are cleared FIRST: a DB failure mid-delete must not
        leave the plaintext token cached for the rest of the process.
        """
        self._memory.pop(account, None)
        self._runtime_sources.pop(account, None)
        self._db_accounts.discard(account)
        existed = await self._repo.delete(account) \
            if self._repo is not None else False
        return existed

    # ------------------------------------------------------------- status
    def status(self, account: str) -> str:
        """'db' | 'missing' — display-safe (never the token).

        Sync by design (status_dict call sites): answers from the in-memory
        account set kept fresh by store/delete/refresh. Only 'db' and
        'missing' exist — the environment path is GONE at runtime.
        """
        if account in self._db_accounts:
            return "db"
        return "missing"

    async def import_from_env(self, env_tokens: dict,
                              overwrite: bool = False) -> dict:
        """Explicit one-shot import of legacy .env tokens (spec §17).

        Never silently overwrites: accounts with an existing DB credential
        are skipped unless ``overwrite``. Returns a report dict —
        ``{imported: [...], skipped: [...], failed: {...}}`` — with
        account NAMES only (no token material).
        """
        imported, skipped, failed = [], [], {}
        known = await self._repo.accounts_with_credential() \
            if self._repo else []
        for account, token in env_tokens.items():
            if not token:
                continue
            if account in known and not overwrite:
                skipped.append(account)
                continue
            try:
                await self.store(account, token)
                imported.append(account)
            except Exception as exc:
                failed[account] = f"{type(exc).__name__}: {exc}"
        return {"imported": imported, "skipped": skipped, "failed": failed}

    def __repr__(self) -> str:  # safety net: never leak material
        return f"<CredentialService repo={self._repo is not None} " \
               f"cached={sorted(self._memory)} key={str(self._key_path)!r}>"

__all__ = ["CredentialService", "KEY_ENV", "DEFAULT_KEY_PATH"]
