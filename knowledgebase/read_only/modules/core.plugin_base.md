# Module `core.plugin_base`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/plugin_base.py`

```
Plugin contract: base class, runtime context, services protocol (spec §4-6).

Ported from the audited ``core/plugin_base.py`` (git aa76da4) onto the rewrite
spine:

  * settings manifests now use :class:`core.settings_manager.Setting`,
  * Discord I/O goes ONLY through the :class:`PluginServices` protocol
    (implemented per account by the account manager, A6) — plugins never
    touch a client, a limiter or the bus directly,
  * the runtime context is a plain dataclass built by the plugin manager
    (one context per (account, plugin) pair).

Plugin contract (unchanged from the audited original):
  * declare metadata (``name`` — lowercase ``[a-z0-9_-]+`` — ``version``,
    ``description``) and a settings manifest (``settings``),
  * implement lifecycle hooks (``on_load``/``on_enable``/``on_disable``/
    ``on_unload``) and :meth:`PluginBase.handle_event`,
  * catch :class:`core.errors.FlowBlocked` around any multi-step flow, log
    ONE clean abort line and return — per-account isolation is the
    dispatcher's job, failing clean and quiet is the plugin's (reference:
    ``tests/fixtures/sim_plugins/soulcopy/plugin.py``, a test-only
    workload fixture).

CONTRACT NOTE (A3 decision, documented for A6): the pinned PluginServices
protocol gained ``open_dm(user_id) -> int`` — flow plugins need the Mudae
DM channel id to filter collected pages. The account manager implements it
(the underlying DiscordClient protocol already exposes ``open_dm``).

SELF-COMMAND DECLARATION (per-account command interface): a plugin may
declare a ``COMMANDS`` class attribute — a tuple of :class:`CommandSpec` —
making its verbs available as ``<prefix><name>`` self-commands on every
account it is ASSIGNED to and ENABLED on (e.g. ``sm!rollstart``). Handlers
run through the SAME paced/isolated pipeline as plugin code: replies go
through ``ctx.services.send_message`` (never a raw client), a crashing
handler is contained by the executor, and the plugin manager binds each
declared handler to the per-account plugin instance before invocation
(handlers may be plain methods — see CommandSpec).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `StatCard` | dataclass | yes | One metric card on the Home tab (plugin-contributed). |
| `PanelSection` | dataclass | yes | One section of a plugin sidebar panel (structured data only). |
| `UIPanel` | dataclass | yes | A plugin's sidebar panel — appears as a nav entry + content area. |
| `PluginBase` **PLUGIN API** | abc | yes | Contract every plugin module must fulfil. |
| `PluginContext` **PLUGIN API** | dataclass | yes | Everything a plugin may touch at runtime. |
| `ArgSpec` | dataclass | yes | One declarative positional argument of a self-command. |
| `CommandSpec` | dataclass | yes | One plugin-declared self-command (Mudae-style grammar). |
| `CommandSettingsApi` | protocol | yes | Validated settings surface for self-command handlers. |
| `CommandContext` | dataclass | yes | Everything a self-command handler may touch at runtime. |
| `PluginServices` **PLUGIN API** | protocol | yes | Discord I/O surface handed to plugins (implemented by A6 per account). |

### `PluginBase` — PLUGIN API

- `async def ui_stats(self, ctx: 'PluginContext') -> Sequence['StatCard']`
  - Stat cards this plugin contributes to the Home tab.
- `async def ui_panels(self, ctx: 'PluginContext') -> Sequence['UIPanel']`
  - Sidebar panels this plugin contributes.
- `def initialize_dashboard_ui(self, ui: 'DashboardUIToolset') -> None`
  - Optional hook for declaring this plugin's dashboard interface.
- `def attach_dashboard_ui(self, layout: 'Layout') -> None`
  - Attach a completed UI layout to the framework.
- `async def on_load(self, ctx: 'PluginContext') -> None` *(plugin API)*
  - Called once when the plugin is first enabled on an account.
- `async def on_enable(self, ctx: 'PluginContext') -> None` *(plugin API)*
  - Called every time the plugin becomes active (after on_load).
- `async def on_disable(self, ctx: 'PluginContext') -> None` *(plugin API)*
  - Called when the plugin is disabled or the account stops.
- `async def on_unload(self, ctx: 'PluginContext') -> None` *(plugin API)*
  - Called when the plugin is removed from the account (last call).
- `async def handle_event(self, ctx: 'PluginContext', event: 'Event') -> None` *(plugin API)*
  - Dispatch point for every ``MessageReceived`` seen by the account.

### `PluginContext` — PLUGIN API


### `CommandSettingsApi`

- `def schema(self) -> Sequence['Setting']`
  - The plugin's setting schema (declared order).
- `async def effective(self) -> Dict[str, Any]`
  - Merged effective settings incl. the reserved ``channels`` scope.
- `async def set(self, key: str, value: Any) -> Any`
  - Validate + persist one per-account override; returns typed value.
- `async def clear(self, key: str) -> Any`
  - Remove one per-account override; returns the newly-effective value.
- `async def binding(self) -> Optional[str]`
  - Name of the account's bound profile for this plugin (None = none).
- `async def profiles(self) -> List[Dict[str, Any]]`
  - The plugin's available profiles (``name``/``description`` rows).

### `PluginServices` — PLUGIN API

- `async def send_message(self, channel_id: int, content: str, *, priority: Priority=Priority.LOW) -> None`
  - Send to a guild/channel id through the task pipeline.
- `async def send_dm(self, user_id: int, content: str, *, priority: Priority=Priority.LOW) -> None`
  - Send a DM to a user id through the task pipeline
- `async def open_dm(self, user_id: int) -> int`
  - Resolve (opening if needed) the DM channel id for a user.
- `async def add_reaction(self, channel_id: int, message_id: int, emoji: str, *, priority: Priority=Priority.NORMAL) -> None`
  - React to a message through the paced pipeline (kakerareact).
- `async def click_button(self, channel_id: int, message_id: int, custom_id: str, *, priority: Priority=Priority.NORMAL) -> None`
  - Click a message component by custom_id (kakeraloot) — paced and
- `async def fetch_user(self, user_id: int) -> Any`
  - Resolve a user (cached per account in the database); None when
- `async def cooldown_set(self, key: str, duration_s: float, *, source: str='plugin', reason: str='', metadata: Optional[Dict[str, Any]]=None) -> Dict[str, Any]`
  - Start (or replace) a persistent cooldown for this account+plugin.
- `async def cooldown_remaining(self, key: str) -> float`
  - Seconds left on a cooldown (0.0 when none/expired).
- `async def cooldown_active(self, key: str) -> bool`
  - True when a cooldown is currently active for this key.
- `async def cooldown_clear(self, key: str) -> bool`
  - Remove one cooldown; True when it existed.
- `async def cooldown_list(self) -> List[Dict[str, Any]]`
  - Every ACTIVE cooldown of this account+plugin, soonest first.
- `async def state_get(self, key: str, default: Any=None) -> Any`
  - Read one persisted value from this account+plugin scope.
- `async def state_set(self, key: str, value: Any) -> None`
  - Persist one JSON-serializable value in this account+plugin scope.
- `async def state_delete(self, key: str) -> bool`
  - Delete one value from this account+plugin scope; True when it
- `async def wait_for_message(self, check: Callable[['MessageReceived'], bool], timeout: float) -> Optional['MessageReceived']`
  - Wait for one matching inbound message; None on timeout.
- `async def collect_messages(self, check: Callable[['MessageReceived'], bool], idle_timeout: float) -> List['MessageReceived']`
  - Collect matching inbound messages until ``idle_timeout`` passes
- `def publish(self, event: 'Event') -> None` — side effects: `publishes_event`
  - Publish an event on the shared bus (fire-and-forget safe).
- `def validate_channel(self, channel_id: int) -> None`
  - Raise :class:`core.errors.TargetGone` when the channel is not
- `def describe_channel(self, channel_id: int) -> str`
  - Human-readable channel label (``#name`` / DM partner).
- `def credentials(self) -> 'Credentials'`
  - Fleet credentials (mudae id, owner ids) — never hardcoded.
- `def account(self) -> str`
  - Account name this facade belongs to.
- `def plugin(self) -> str`
  - Plugin name this facade was created for.
- `def settings(self) -> Dict[str, Any]`
  - Resolved settings of this (account, plugin) pair.

## Functions

- `def parse_command_args(spec: 'CommandSpec', argv: List[str]) -> Dict[str, Any]`
  - Parse ``argv`` against the command's declared ArgSpecs.
- `def usage_line(spec: 'CommandSpec') -> str`
  - Auto-generated usage: ``<a> <b> [c...]`` from the ArgSpecs
