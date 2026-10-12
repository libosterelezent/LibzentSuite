# Module `core.framework_credentials`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `core/framework_credentials.py`

```
FrameworkCredentials — the DB-first credentials view plugins see.

Handed to plugin services as ``services.credentials`` in place of the
raw env ``Credentials``: plugins keep reading ``.owner_user_ids`` and
``.mudae_bot_id`` unchanged, but the values now come from the
admin-managed store (``AccessManager``), falling back to env when the
DB value is unset. The self-command prefix moves to the per-account
access row (see adapters/discord/client.py).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `FrameworkCredentials` | class | yes | Duck-typed Credentials replacement (DB-first, env fallback). |

### `FrameworkCredentials`

- `def owner_user_ids(self) -> FrozenSet[int]`
  - Admin ids (the replacement meaning of 'owners').
- `def mudae_bot_id(self) -> Optional[int]`
  - The Mudae bot user id — DB setting first, env fallback.
- `def require(self, key: str) -> str`
  - Env passthrough for keys the access model doesn't own.
