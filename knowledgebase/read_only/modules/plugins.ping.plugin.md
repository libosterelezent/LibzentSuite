# Module `plugins.ping.plugin`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `plugins\ping\plugin.py`

```
Ping plugin: sends a provided message to a chosen channel.

The first REAL production plugin (the framework ships none by design —
this is the operator's first drop-in). One capability, done safely:

  * ``<prefix>ping <channel_id> <text...>`` — self-command that sends
    ``text`` to ``channel_id`` through the SAME paced pipeline as all
    plugin traffic (ctx.services.send_message — never a raw client),
  * ``on_ping`` dashboard-UI action does the same from the dashboard,
  * ``FlowBlocked`` safety: a gone/forbidden channel aborts ONE clean
    log line — the plugin contract (core/plugin_base.py docstring).

Named ``ping`` (not ``say``) because the framework reserves ``say`` as
a retired RemoteControl mention verb — a plugin re-declaring it is
dropped at collection time.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `PingPlugin` | class | yes | Send a provided message to a chosen channel (paced, audited). |

### `PingPlugin`

- `async def on_load(self, ctx: PluginContext) -> None`
- `async def on_enable(self, ctx: PluginContext) -> None`
- `async def on_disable(self, ctx: PluginContext) -> None`
- `async def on_unload(self, ctx: PluginContext) -> None`
- `async def handle_event(self, ctx: PluginContext, event: Event) -> None`
- `async def ui_stats(self, ctx: 'PluginContext')`
- `def initialize_dashboard_ui(self, ui: 'DashboardUIToolset') -> None`
  - A small page: channel + text form, one Send button.
