# Module `database`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `database\__init__.py`

```
Persistence layer: SQLite models, schema migrations, repositories.

Layout (spec §7 — "use a proper database instead of one large JSON file"):
  models.py       dataclasses exchanged with the rest of the framework (spine)
  schema.py       ordered SQL migrations (spine)
  db.py           Database: connection + migration runner (A2)
  repositories/   one module per table family (A2)

Tables: accounts, account_settings, plugins, plugin_settings, plugin_states,
profiles, profile_plugins, account_profiles, rate_limit_policies, audit_logs,
logs, task_history, kv, schema_migrations.
```
