# Module `adapters.simulator`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `adapters/simulator/__init__.py`

```
Simulator adapter: the offline half of the Discord boundary.

Exports:
  * SimWorld         — in-memory Discord world (users, channels, DMs,
    scripted responder, one-shot 429s, channel deletion)
  * SimClient        — adapters.base.DiscordClient implementation over the
    world (one per simulated account)
  * Scenario/SCENARIOS — full-stack scenario registry (spec §16)
  * SimulationRunner — builds a complete ephemeral stack per scenario run

Everything here runs offline with SYNTHETIC ids only — no network, no tokens.
```
