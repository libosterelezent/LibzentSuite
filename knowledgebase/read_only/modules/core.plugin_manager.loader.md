# Module `core.plugin_manager.loader`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\plugin_manager\loader.py`

```
Plugin discovery: finds PluginBase subclasses under a plugin directory.

Ported from the audited ``core/plugin_loader.py`` (git aa76da4). Layout
conventions (relative to the plugins dir):

  * ``<dir>/<plugin_name>/__init__.py``  -> package plugin (preferred;
    submodules can use relative imports, e.g. ``from .textutils import ...``)
  * ``<dir>/<plugin_name>.py``           -> single-file plugin

Modules are loaded under a **synthetic parent package** (a unique module
object whose ``__path__`` is the plugins dir), so package plugins get
working relative imports without any ``sys.path`` hacks and without
colliding with real packages.

Audit-hardened behaviour kept from the original:
  * strict ASCII plugin names (``[a-z0-9_-]+``) — ``isalnum()`` accepted
    Unicode slugs that could never round-trip through settings keys,
  * duplicate plugin names and duplicate setting keys are hard errors,
  * dotted directory names are skipped with a warning (they cannot be
    imported as packages — a confusing ImportError otherwise),
  * a module exporting no PluginBase subclass logs a warning instead of
    being silently skipped.

All discovery failures raise :class:`core.errors.PluginError` with a
human-readable message (the old loader's PluginLoadError folded into the
shared domain error vocabulary).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `PluginRecord` | dataclass | yes | A discovered plugin: metadata class + where it came from. |
| `PluginLoader` | class | yes | Discovers and instantiates plugins from a directory tree. |

### `PluginLoader`

- `def directory(self) -> Path`
  - The directory this loader scans.
- `def discover(self) -> Dict[str, PluginRecord]`
  - Scan the plugin dir; returns ``name -> record`` (fresh scan).
- `def names(self) -> List[str]`
  - Sorted plugin names (discovers lazily on first call).
- `def record(self, name: str) -> PluginRecord`
  - Record lookup; raises PluginError for unknown plugins.
- `def create(self, name: str) -> PluginBase`
  - Fresh instance of a plugin (one per account/plugin pair).
- `def manifests(self) -> List[dict]`
  - JSON-safe manifests (metadata + settings schema) for CLI/UI.
