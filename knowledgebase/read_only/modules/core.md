# Module `core`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\__init__.py`

```
LibzentSuite core framework.

Component layout (see INTEGRATION.md and ARCHITECTURE.md):
  core.account_manager    Account (first-class), AccountManager, DiscordCache
  core.plugin_manager     plugin discovery/lifecycle/dispatch (+ core.plugin_base)
                          are DIRECT account→plugin assignments now)
  core.settings_manager   shared settings schema + layered resolution
  core.rate_limit_manager 4-level rate policies, pacing, server limits, warnings
  core.task_scheduler     centralized task pipeline (priority/retry/timeout)
  core.event_bus          typed pub/sub with handler isolation
  core.logging_service   structured logging for every component
  core.api                CoreAPI — the ONE facade both interfaces call
```
