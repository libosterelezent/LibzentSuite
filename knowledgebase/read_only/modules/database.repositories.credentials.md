# Module `database.repositories.credentials`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `database/repositories/credentials.py`

```
Encrypted-credential repository (spec §12/§13).

Stores Fernet ciphertext ONLY. The key lives outside the database (see
core.credential_service) — a stolen DB yields no usable tokens. Plaintext
never enters this layer: callers hand ciphertext in and get ciphertext
out; decryption is the CredentialService's controlled job.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `CredentialsRepository` | class | yes | CRUD over the ``credentials`` table (ciphertext blobs). |

### `CredentialsRepository`

- `async def store(self, account: str, ciphertext: bytes) -> None`
  - Insert or replace the stored ciphertext for an account.
- `async def load(self, account: str) -> Optional[bytes]`
  - The stored ciphertext, or None when the account has none.
- `async def delete(self, account: str) -> bool`
  - Remove the credential; True when one existed.
- `async def accounts_with_credential(self) -> list`
  - Account names that have a stored credential (status display).
