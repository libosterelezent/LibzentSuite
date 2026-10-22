# Module `adapters.discord`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `adapters\discord\__init__.py`

```
Real Discord adapter: the live-client half of the adapter boundary.

Exports:
  * RealDiscordClient — adapters.base.DiscordClient over a lazily imported
    user-account-capable Discord library (discord.py-self)
  * RealClientFactory — adapters.base.ClientFactory building live clients

The ``discord`` import happens INSIDE connect(), so importing this package is
always safe offline (simulator, tests, CLI dry-runs).
```
