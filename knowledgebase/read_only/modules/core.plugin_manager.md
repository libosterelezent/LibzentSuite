# Module `core.plugin_manager`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/plugin_manager/__init__.py`

```
Plugin Manager (spec §2): discovery, per-account instances, dispatch.

Public surface: PluginManager (+ PluginRuntime). The loader lives in
``core.plugin_manager.loader`` (PluginLoader/PluginRecord), the plugin
contract in ``core.plugin_base`` (PluginBase/PluginContext/PluginServices/
CommandSpec/CommandContext) and the per-account self-command registry in
``core.plugin_manager.commands`` (SelfCommandHub + framework pg/say/help).
```
