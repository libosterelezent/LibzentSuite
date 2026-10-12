# Module `plugins`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `plugins/__init__.py`

```
Plugins directory — each subdir is a framework plugin.

The framework ships with NO bundled plugins: this directory may be empty
(a lone ``__init__.py``) and everything still boots — the loader simply
discovers zero plugins. Drop a plugin package here (or point
``--plugins`` at any directory) to add one.

Plugin contract recap (see core/plugin_base.py for the base class):
  * declare metadata + a settings manifest; reach Discord ONLY through
    ctx.session (rate-limited, audited, cache-aware);
  * flow safety: when a target is unusable, the session/cache raise a
    core.errors.FlowBlocked subclass (TargetGone / AccessDenied). Catch
    FlowBlocked around your WHOLE flow, log one clean abort line and return —
    other accounts keep running (container isolation). Reference
    implementation: tests/fixtures/sim_plugins/soulcopy/plugin.py (a
    test-only workload fixture).
```
