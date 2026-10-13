# Module `core.config`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/config.py`

```
Project configuration loading (config/ directory, all optional).

Files: ``global_settings.json`` (plugin setting overrides — layer 2 of
the settings merge, account layer wins), ``rate_limits.json`` (the policy
format below), and ``accounts/<name>.json`` (one account bootstrap per
file; never tokens — only the ``.env`` key reference).

Policy file shape::

    {"global":    {action: {"interval": 2.0, "jitter": [0.1, 0.5]}},
     "accounts": {name: {action: {...}}},
     "lane":      {"enabled": true, "prefixrate": 1.5}}
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `ProjectConfig` | dataclass | yes | Everything the framework needs besides credentials + the database. |

## Functions

- `def load_config(config_dir: str | Path) -> ProjectConfig`
  - Load the config dir; malformed pieces fail LOUD (typo protection).
