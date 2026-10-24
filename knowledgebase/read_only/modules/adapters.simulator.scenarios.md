# Module `adapters.simulator.scenarios`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `adapters\simulator\scenarios.py`

```
Scenario definitions for the offline simulation suite (spec §16).

Each :class:`Scenario` bundles everything the runner needs:
  * ``policies``  — rate policies applied BEFORE the run (interval + jitter);
    the runner clamps every interval to the 1.5s safety floor — simulations
    never configure unsafe pacing,
  * ``world_setup`` — builds the synthetic topology (users, the trigger
    channel, the scripted Mudae responder and its pages),
  * ``script``     — drives the built stack (usually: the owner types the
    soulcopy fixture's trigger, then the world settles),
  * ``checks``     — pure post-mortem over (audit dicts, world) returning
    human-readable failure strings ([] == pass).

All ids are SYNTHETIC (module constants in adapters.simulator.world); real
Discord ids/tokens only ever live in .env.

Documented decisions (contract ambiguities — see INTEGRATION.md ground rules):
  * Retry evidence in ``server_429_respected`` is verified primarily from
    WORLD observables (the recorded blocked attempt followed by a later
    successful delivery). Audit notes mentioning attempts/retries are
    accepted as extra evidence but NOT required: their exact wording is not
    pinned by the A5/A6 contract, so a wording-based hard requirement could
    false-fail at integration.
  * Plugin demo settings (seconds, not minutes — production pacing lives in
    config/) ride into the stack as the SettingsManager GLOBAL layer via
    :data:`DEMO_PLUGIN_SETTINGS`, exactly like the audited SimulatedCluster.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `Scenario` | dataclass | yes | One full-stack simulation scenario (registry entry). |
| `ScenarioAPI` | dataclass | yes | The facade handed to a scenario script (api-less stack driver). |

## Functions

- `def quiet_window() -> float`
  - Quiet seconds the scripts wait for: DM read timeout + headroom.
