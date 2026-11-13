# LibzentSuite — TODO + Ideas

**Reorganized 2026-11-13** to reflect the post-dashboard-removal,
post-Mudae-removal codebase. Everything here describes what the
repository **currently holds** — no stale references to removed systems.

---

## Core Design Goal

    One Core API (ControlAPI)
          |
      +---+---+
      |       |
     CLI     Future UI (FastAPI + Flet)

Both interfaces manipulate the exact same:
- Accounts and plugin assignments
- Plugin profiles (settings presets)
- Tasks and scheduling
- Rate-limit policies and the safety floor
- Credentials (encrypted at rest)
- Logs and audit trail
- Simulation system
- Fleet orchestration

Plugins remain independent from the interface layer.

---

## Current State (2026-11-13)

**What exists and is tested (1,673 tests green, CI green):**
- Core framework: bootstrap → ControlAPI → managers → repositories → adapters
- Multi-account Discord orchestration (discord.py-self, lazily imported)
- Plugin system: discovery, lifecycle hooks, settings, self-commands, per-account instances
- Plugin profiles: named settings presets, account-to-preset bindings
- AccountPluginData: per-channel operational data (own SQLite store, optimistic versioning)
- Sync groups: persistent membership + shared pacing clock
- Fleet orchestration: task lifecycle, handoff, cooldowns, reconciler, dispatch guard
- Rate limiting: 1.5 s safety floor, learned defaults, server-cooldown handling, send lane
- Credential service: Fernet-encrypted at rest, key outside DB, write-only in UI
- Parser registry: generic, empty, game-packs plug in via `register_parser()`
- CLI: REPL + one-shot commands (the only interface currently)
- Simulator: complete offline Discord replacement with scenarios
- Build: uv-locked, ruff-gated, mypy-advisory (1,086 baseline), CI on GitHub Actions

**What was removed (2026-11-13):**
- The entire dashboard interface (custom HTTP server, WebSocket, SPA, builder contract)
- The Mudae parser pack (~3,000 lines of game-specific recognition)
- All Mudae references (renamed to "primary bot" — game-agnostic)

---

## Active Queue

[ ] **FastAPI core API** — replace the deleted dashboard's REST surface
    with FastAPI routers calling the existing ControlAPI. Pydantic
    request/response models. Native WebSocket for live events.
    Study: `docs/pivot-audit-fastapi-flet.md` (phase 2)

[ ] **Flet UI** — replace the deleted SPA with a Python-native UI.
    Pages per module (accounts, fleet, orchestration, extensions,
    control plane, system). Plugin UI contribution contract redesign.
    Study: `docs/pivot-audit-fastapi-flet.md` (phase 3)

[ ] **Switch to better.py-self** — drop-in fork with memory caps,
    cache persistence (75 ms warm restarts), throttle events, latency
    EWMA, shared-scope 429 correctness. Study:
    `docs/better-py-self-integration.md`

[ ] **PHASE C LIVE DEBUG** — bravo re-credentialed and running; alpha
    still needs a fresh credential. Remaining: admin-command flow
    B -> A, ordered full walkthrough.

---

## Open Ideas (unscheduled)

### Fleet enhancement

[ ] Metric rollup wiring: declared metric hints (sum|last) surfaced
    through the future FastAPI dashboard — the opt-in GLOBAL view over
    AccountPluginData (the store + LOCAL view shipped; only the
    aggregation surface is missing)
[ ] cooldown_key policy mapping: TaskDefinition.cooldown_key read
    through to FleetPolicy so a task's pacing key can live in its
    per-channel data document (the field exists, the mapping is unwired)
[ ] FleetTaskHandler: the capability protocol is fully implemented and
    tested (33 fleet tests) — zero plugins currently use it; the first
    real fleet plugin validates the contract end-to-end

### Rate limiting (better.py-self integration)

[ ] Bridge `on_http_throttle` to `RateLimitManager.report_server_limit()`
    — the REAL throttle signal for user accounts (library absorbs 429s
    internally; we currently only see the ones it doesn't). Wire: hook the
    event in `adapters/discord/client.py` → publish `RateLimitTriggered`
    on the EventBus → limiter calls `report_server_limit()`.
[ ] Add per-route latency EWMA congestion watch (`http_route_latencies()`)
    — queuing inflation, not 429s, is how the send ceiling manifests.
    Add an EWMA monitor to `core/rate_limit_manager/engine.py` that
    reads `client.http_route_latencies()` and warns when the send route
    exceeds a threshold.
[ ] Add bounded probe-down recovery (decay backoff ≤10% after 2 min clean,
    never below the base interval) — matches the fork's lane governor recipe
    (`examples/lane_governor.py`). Apply to `RateLimitManager`'s
    cooldown recovery path.
[ ] Expose `cache_stats()` through ControlAPI — add a `system_cache_stats()`
    method that aggregates per-account `client.cache_stats()` snapshots
    for the future dashboard's System page.
[ ] Add memory caps + cache persistence to the Client constructor in
    `adapters/discord/client.py::_build_client()`:
    `max_member_cache=500, max_total_member_cache=5000,
    persistence_path=f"state/{account}-cache.bin",
    gc_freeze=True, dispatch_backlog_warning=500`.

### Command system (ext.commands deep adoption)

[ ] Move the authorization gate to `bot.add_check()` in
    `adapters/discord/client.py` — currently `_dispatch_self_command()`
    manually checks `allowed_invoker_async(ctx.author.id)` AFTER the
    library has already parsed arguments. Moving it to a global check
    (`bot.add_check(access_gate)`) means unauthorized commands are
    rejected before any parsing work. `on_command_error` already
    handles `CommandNotFound` silently — extend to catch `CheckFailure`.
    AccessManager stays as the domain owner (per-account allowlists,
    DB persistence); only the invocation point moves.
    NOTE: do NOT adopt `commands.cooldown()` — our cooldowns are
    per-account, DB-persisted, fleet-aware domain state.

[ ] Replace `_parse_invocation()` with library-native argument parsing.
    Current: `adapters/discord/client.py:1100` manually tokenizes
    `"sm!rollstart 123"` into `(["123"], "123")` using regex + manual
    prefix stripping. The library's parameter parsing already does this
    via type annotations on the command callback:
    ```python
    async def _invoke(ctx, target: discord.User, count: int = 1):
        # ctx.args/ctx.kwargs already parsed and typed
    ```
    This eliminates `_parse_invocation` (~60 lines) and the manual
    `ArgSpec` type-coercion path in `core/plugin_base.py::_convert_arg`.

[ ] Adopt library converters in `ArgSpec` — add a `converter` field
    (`discord.Member | discord.User | discord.TextChannel | str | int`)
    that maps to the library's converter system. When a plugin declares
    `ArgSpec("target", discord.User, ...)`, the library resolves the
    argument from ID/mention/name automatically. Available converters:
    `MemberConverter, UserConverter, TextChannelConverter, RoleConverter,
    EmojiConverter, GuildConverter, MessageConverter` + `Greedy[T]`,
    `Range[int, min, max]`, `clean_content`.

[ ] Simplify `_invoke` pass-through — with `bot.add_check()` handling
    authorization and library parsing handling arguments, the thin
    command wrapper in `_register_self_command()` becomes:
    ```python
    async def _invoke(ctx):
        reply = await adapter._paced_reply(ctx, ctx.command.name)
    ```
    No auth check, no manual parsing, no `_dispatch_self_command`.

[ ] Framework verbs as Cogs — `core/plugin_manager/commands.py`
    (922 lines) manually bridges `CommandSpec` → `bot.command(name=...)`
    via `_register_self_command()` + `_rebuild_self_commands()` diff
    tracking. Cogs replace this:
    ```python
    class FrameworkVerbs(commands.Cog):
        @commands.command(name="status")
        async def status_cmd(self, ctx):
            reply = await self.api.status()
            await ctx.send(format_status(reply))

        @commands.command(name="estop")
        async def estop_cmd(self, ctx):
            await self.api.emergency_stop_all(surface="chat")
            await ctx.send("emergency stop engaged")
    ```
    Benefits: `add_cog()/remove_cog()` for hot-reload (replaces our
    diff tracking), `cog_command_error()` for per-module error handling,
    built-in help via `help_command`, cleaner code organization.
    Plugin `COMMANDS` declarations still go through the hub for
    per-account assignment/enable gating — cogs are for the framework
    verbs only (status, accounts, plugins, ratelimit, lane, tasks,
    estop, simulation, adminlist, adminperms, adminprefix).

[ ] `before_invoke` for audit logging — currently audit rows are written
    inside `_dispatch_self_command` after manual parsing. Move to:
    ```python
    @bot.before_invoke
    async def audit_invocation(ctx):
        adapter._audit_chat_command(ctx.command.qualified_name, ctx.author.id)
    ```
    Runs after checks pass, before the command callback — clean separation
    of authorization (check), audit (hook), and execution (callback).

[ ] Leverage `Context` methods — our code manually calls
    `services.send_message(channel_id, text)` for replies. The library's
    `ctx.send()` already routes through the client's send path. Bridge:
    override `ctx.send` in `_register_self_command` to route through our
    paced pipeline (scheduler → limiter → adapter), preserving the
    safety floor while using the library's API surface.

### v2 Architecture (design-only, no backend)

[ ] Workspaces: organizational boundary above accounts
[ ] Collections: flexible user-defined account groupings
[ ] Execution Plans / Schedules: higher-level orchestration above tasks
[ ] Workspace-scoped configuration (Global → Workspace → Account)

---

## Codebase Hygiene

[ ] mypy strict ratchet: 1,086 errors → 0 (fleet family first, then
    framework_ui, then dashboard_ui components, then api.py)
[ ] `core/remote_control.py` (365 lines): only the simulator's
    `control_mention` scenario uses it — delete when that scenario
    is retired

---

## Removed Systems (context for new contributors)

| System | Removed | Superseded by |
|---|---|---|
| ProfileManager (permission bundles) | 2026-10-22 | Direct account→plugin assignment (`account_plugins` table) |
| Mudae parser pack | 2026-11-13 | Generic parser registry (`parsers/manager.py`, ships empty) |
| Dashboard (custom HTTP/WS/SPA) | 2026-11-13 | FastAPI + Flet (planned) |
