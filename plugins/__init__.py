"""Plugins directory — each subdir is a framework plugin.

Plugin contract recap (see core/plugin_base.py for the base class):
  * declare metadata + a settings manifest; reach Discord ONLY through
    ctx.session (rate-limited, audited, cache-aware);
  * flow safety: when a target is unusable, the session/cache raise a
    core.errors.FlowBlocked subclass (TargetGone / AccessDenied). Catch
    FlowBlocked around your WHOLE flow, log one clean abort line and return —
    other accounts keep running (container isolation). Reference
    implementation: plugins/soulcopy/plugin.py.
"""
