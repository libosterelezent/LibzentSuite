# Plugin Map

> The framework ships with **NO bundled plugins** — `plugins/` may stay
> empty and everything still boots (the loader discovers zero plugins).
> Generated declarations (read-only, commit-pinned):
> [`../read_only/plugins/`](../read_only/plugins/). System overview:
> [../PLUGIN_SYSTEM.md](../PLUGIN_SYSTEM.md) · API:
> [../PLUGIN_API.md](../PLUGIN_API.md) · full writing guide:
> `docs/plugins/writing_plugins.rst`.

The reference implementations live in the test suite (never discovered by
a production boot):

| Fixture | Kind | Purpose | Source |
|---|---|---|---|
| `probe` | loader/manager/UI/command fixture | the minimal well-formed plugin: settings manifest, declared `COMMANDS` (positional + greedy ArgSpec grammar), `ui_stats`/`ui_panels` providers | `tests/fixtures/plugins/probe/plugin.py` |
| `soulcopy` | simulation workload | the full multi-step flow plugin the offline scenarios drive (DM page collection, chunked re-send) | `tests/fixtures/sim_plugins/soulcopy/plugin.py` |
| `echo`, `spy` | minimal fixtures | event/dispatch assertions | `tests/fixtures/plugins/{echo,spy}.py` |

The former `plugins/roll` and `plugins/soulcopy` packages were REMOVED
(permanently) when the framework moved to shipping zero plugins.

---

## How to add a new plugin

1. **Create the module** under `plugins/`:
   * `plugins/<name>/__init__.py` — package plugin (preferred; submodules may use relative
     imports), or
   * `plugins/<name>.py` — single-file plugin.
2. **Name rules**: `<name>` must fully match `[a-z0-9_-]+` (the directory/file must house a
   `PluginBase` subclass whose `name` ClassVar matches the same rule and is unique); duplicate
   plugin names and duplicate setting keys are hard `PluginError`s at discovery.
3. **Subclass `core.plugin_base.PluginBase`** in that module (classes defined elsewhere are not
   discovered): declare `name`, `version`, `description`, `settings` (a tuple of
   `core.settings_manager.Setting`), optionally `COMMANDS` (a tuple of `CommandSpec` — the
   plugin's Discord self-command verbs, ArgSpec-declared grammar) and the UI-extension hooks
   `ui_stats`/`ui_panels`, and implement what you need of `on_load` / `on_enable` /
   `on_disable` / `on_unload` / `handle_event`.
4. **Follow the contract**: filter fast in `handle_event`, gate every trigger on the
   access/owner view, validate channels before acting, wrap multi-step flows in one
   `try/except FlowBlocked` with a single abort line
   ([../PLUGIN_ANTIPATTERNS.md](../PLUGIN_ANTIPATTERNS.md)).
5. **Restart / rescan**: discovery runs at boot (`PluginManager.discover()`); the loader also
   upserts plugin metadata rows so both UIs see the new manifest. A discovered plugin runs on an
   account only via a DIRECT assignment — `accounts plugin-add <account> <plugin>`
   (`accounts_plugin_assign` over the `account_plugins` table), which applies immediately.
   Bind a **settings profile** per account (Plugins/Profiles tab or
   `/api/accounts/plugin-profile`) to ship a reusable settings bundle incl. a channel scope.
6. **Test offline**: simulation scenarios run the real stack against `SimWorld` with zero
   network ([../ADAPTERS.md §4](../ADAPTERS.md)); the `probe` fixture is the copy-and-adapt
   template for grammar/commands/UI, the `soulcopy` fixture the full-flow template
   ([../PLUGIN_EXAMPLES.md](../PLUGIN_EXAMPLES.md)).

Reference deep-dive: [../PLUGIN_SYSTEM.md](../PLUGIN_SYSTEM.md) (incl.
§7a self-commands and §7b UI extension).
