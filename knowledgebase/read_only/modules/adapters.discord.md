# Module `adapters.discord`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `adapters/discord/__init__.py`

```
Real Discord adapter: the live-client half of the adapter boundary.

Exports:
  * RealDiscordClient — adapters.base.DiscordClient over a lazily imported
    user-account-capable Discord library (discord.py-self)
  * RealClientFactory — adapters.base.ClientFactory building live clients

The ``discord`` import happens INSIDE connect(), so importing this package is
always safe offline (simulator, tests, CLI dry-runs).
```
