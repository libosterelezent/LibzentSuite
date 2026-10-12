# Module `core.credentials`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `core/credentials.py`

```
Credential handling: every token and user/bot ID lives in .env, never in code.

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
  * the framework-level ``MUDAE_BOT_ID`` setting (DB value with the env
    value as a read-only fallback),

and :class:`FrameworkCredentials` is the DB-first view handed to plugin
services as ``services.credentials`` (plugins keep reading
``.owner_user_ids`` / ``.mudae_bot_id``; the values now come from the
admin-managed store, falling back to env when the DB value is unset).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `Credentials` | class | yes | Immutable-ish view over a parsed .env payload. |

### `Credentials`

- `def parse_env(text: str) -> Dict[str, str]`
  - Parse .env text: comments, blank lines, optional 'export ', quotes.
- `def from_file(cls, path: str | Path='.env') -> 'Credentials'`
  - Load credentials from a .env file; a missing file yields an empty set.
- `def from_values(cls, values: Mapping[str, str]) -> 'Credentials'`
  - Build credentials directly (tests/simulations with synthetic ids).
- `def snapshot(self) -> Dict[str, str]`
  - Copy of all parsed values (overlaying consumers never mutate us).
- `def get(self, key: str, default: Optional[str]=None) -> Optional[str]`
  - Raw string value or default.
- `def require(self, key: str) -> str`
  - Value or raise CredentialsError (for hard requirements).
- `def token_for(self, account_name: str) -> Optional[str]`
  - MIGRATION-ONLY: reads DISCORD_TOKEN_<NAME> from .env.
- `def mudae_bot_id(self) -> int`
  - Mudae bot user id (env value — LEGACY fallback).
- `def owner_user_ids(self) -> frozenset[int]`
  - Comma-separated OWNER_USER_IDS -> frozenset of ints (empty if unset).
- `def dashboard_token(self) -> Optional[str]`
  - Optional local auth token for the dashboard.
- `def self_command_prefix(self) -> str`
  - Prefix that lets the framework account command ITSELF.
- `def remote_control_enabled(self) -> bool`
  - Mention-prefix management from Discord (REMOTE_CONTROL=0 opts out).
- `def redacted(self) -> Dict[str, str]`
  - Human-safe copy: token-like values masked, never fully shown.
- `def source_path(self) -> Optional[Path]`
  - File the values came from, if any.
