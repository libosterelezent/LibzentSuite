# Module `adapters.simulator`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `adapters\simulator\__init__.py`

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
