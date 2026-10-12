# Module `core.credential_service`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/credential_service.py`

```
CredentialService — encrypted credential storage (spec §12-§14).

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
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `CredentialService` | class | yes | Encrypt-at-rest credential storage over the credentials repository. |

### `CredentialService`

- `async def refresh(self) -> None` — side effects: `db_write`
  - Re-read the stored-credential account list (boot/status display).
- `def fernet(self) -> Fernet`
  - Lazily-initialized cipher (created on first use, not at boot).
- `async def store(self, account: str, token: str) -> None` — side effects: `db_write`
  - Encrypt + persist a credential and cache it for runtime use.
- `async def resolve(self, account: str) -> Optional[str]` — side effects: `db_write`
  - Runtime resolution — DATABASE ONLY (no .env fallback).
- `async def delete(self, account: str) -> bool` — side effects: `db_write`
  - Remove the stored credential + cached plaintext.
- `def status(self, account: str) -> str`
  - 'db' | 'missing' — display-safe (never the token).
- `async def import_from_env(self, env_tokens: dict, overwrite: bool=False) -> dict` — side effects: `db_write`
  - Explicit one-shot import of legacy .env tokens (spec §17).
