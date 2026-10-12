# Module `core.plugin_manager.commands`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `core/plugin_manager/commands.py`

```
Self-command hub: the per-account command registry (Phase 3).

Connects three parties WITHOUT new wiring in the account manager or the
bootstrap (both outside this phase's ownership):

  * the :class:`~core.plugin_manager.PluginManager` (the registry SOURCE)
    publishes an account's command surface whenever assignments or
    enabled-flags change — the same paths that already re-attach plugins
    (``attach_account`` / ``set_enabled`` / ``detach_account``);
  * each account's live Discord client (the registry CONSUMER) subscribes
    on connect and rebuilds its ``ext.commands`` Bot registrations from
    the published spec list;
  * invocations flow back through :meth:`SelfCommandHub.invoke`, which
    delegates to the bound manager's ``execute_self_command`` — handlers
    run with plugin-grade pacing (replies go through the per-plugin
    services facade into the task pipeline, NEVER a raw client send).

FRAMEWORK COMMANDS: ``pg`` and ``say`` (category "Utilities") and the
dynamic ``help`` ("No Category") are declared HERE, not by plugins —
matching Mudae's layout. They are first-class :class:`CommandSpec`s, so
the registry treats them exactly like plugin-declared verbs (and the
``help`` listing is built dynamically from whatever the registry holds —
``snipewatch``-style extras a later plugin declares appear in the help
with zero framework changes).

COLLISION POLICY (deterministic): framework names are reserved, then
plugin specs are collected in manifest order (plugin name sort); the
FIRST registration of a name wins and every later collision is dropped
with a logged warning naming both plugins.

The hub is a process-wide singleton (like the adapter's IdentifyGate):
one framework runs per process. Tests that build several managers bind
the last one (documented last-wins) — executor-level tests can call
``PluginManager.execute_self_command`` directly, bypassing the hub.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `PacingBackend` | class | yes | Bridge the ``prefixrate`` framework verb to the rate limiter. |
| `RegisteredCommand` | dataclass | yes | One registry entry: the (bound) spec + the plugin it came from. |
| `SelfCommandHub` | class | yes | Process-wide account → command-surface registry. |

### `PacingBackend`

- `def get(self, account: str) -> Optional[float]`
- `def set(self, account: str, seconds: float) -> Optional[float]` — side effects: `db_write`
  - Set the ACCOUNT's message interval; returns the previous

### `SelfCommandHub`

- `def bind(self, manager: Any) -> None`
  - Bind the process's plugin manager (last-wins; tests rebind).
- `def unbind(self, manager: Any) -> None`
  - Drop the binding if THIS manager still owns it (shutdown).
- `def refresh(self, account: str) -> None`
  - Recompute one account's registry and notify its subscriber.
- `def forget(self, account: str) -> None`
  - Drop every trace of an account (detached/removed cascade).
- `def subscribe(self, account: str, rebuild: Callable[[List[CommandSpec]], None], stats_provider: Optional[Callable[[], Dict[str, Any]]]=None) -> None`
  - A live client subscribes: register the rebuild callback, then
- `def unsubscribe(self, account: str) -> None`
  - The client disconnected/stopped: drop its callback and stats
- `def entries(self, account: str) -> List[RegisteredCommand]`
  - Current registry entries (fresh recompute — never stale).
- `def specs(self, account: str) -> List[CommandSpec]`
  - Just the specs (help rendering / registration).
- `def runtime_stats(self, account: str) -> Dict[str, Any]`
  - Latest latency/uptime from the account's client ({} = none).
- `async def invoke(self, account: str, name: str, args: List[str], channel_id: int, *, guild_id: Optional[int]=None, invoker_id: Optional[int]=None, raw: str='') -> None`
  - Route one invocation to the bound manager (fully contained).

## Functions

- `def render_help(specs: Sequence[CommandSpec]) -> str`
  - The categorized ``help`` text for one account's spec list.
- `def help_page_count(specs: Sequence[CommandSpec]) -> int`
  - How many pages the current registry's help renders into.
