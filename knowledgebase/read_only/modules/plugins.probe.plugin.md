# Module `plugins.probe.plugin`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `plugins/probe/plugin.py`

```
Probe plugin: discovery/attachment/command-registry test fixture.

The framework itself ships with no bundled plugins; this fixture is the
plugin those suites exercise instead (discovery, per-account
instantiation, settings resolution, self-command declaration and
executor runs). It deliberately does nothing on events.

It is also the REFERENCE for the declarative argument grammar
(:class:`ArgSpec`): ``probeset`` shows the original-MudaeScript-style
positional form (``rollset <profile_name> <roulette> <channel_id>
<userstack> <ignore_keys>``-shaped), ``probestart`` shows the greedy
multi-name form (``rollstart <p1> <p2> <p3>``-shaped).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `ProbePlugin` | class | yes | Minimal well-formed plugin (settings + declared commands). |

### `ProbePlugin`

- `async def on_load(self, ctx: PluginContext) -> None`
- `async def on_enable(self, ctx: PluginContext) -> None`
- `async def on_disable(self, ctx: PluginContext) -> None`
- `async def on_unload(self, ctx: PluginContext) -> None`
- `async def handle_event(self, ctx: PluginContext, event: Event) -> None`
- `async def ui_stats(self, ctx: 'PluginContext')`
- `async def ui_panels(self, ctx: 'PluginContext')`
- `def initialize_dashboard_ui(self, ui: 'DashboardUIToolset') -> None`
  - Builder-based fixture page: the complete path the generic
