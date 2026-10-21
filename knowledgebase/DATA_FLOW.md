# LibzentSuite — Data Flow

This file traces three flows end-to-end with the exact functions they pass
through: (a) an inbound Discord message, (b) an outbound send from a plugin,
and (c) a management command. Every hop names the real symbol from
`knowledgebase/read_only/SYMBOL_INDEX.md`. For who owns what, see
[COMPONENT_MAP.md](COMPONENT_MAP.md); for boot and teardown order,
[RUNTIME_LIFECYCLE.md](RUNTIME_LIFECYCLE.md).

---

## (a) Inbound message: gateway → plugins (and the audit 'in' row)

```
 discord.py-self gateway event (some other user's message)
        │
        ▼  adapters.discord.client.RealDiscordClient.connect registers:
 on_message(message)                       [library callback]
        │  filters self-messages (message.author.id == self.user_id)
        │  dm = isinstance(message.channel, discord.DMChannel)
        ▼
 adapters.model.convert_message(message, dm=dm, viewer_id=self.user_id)
        │  PURE + non-blocking (§15): reads ONLY what the event holds;
        │  embeds/components/attachments/reactions/reference/author/
        │  channel/guild/created_at captured once; raw = library handle
        ▼
 adapters.model.DiscordMessage (frozen rich DTO)
        │  via the callback assigned by
        │  core.account_manager.manager.AccountManager._wire_client._on_message
        ▼
 core.event_bus.MessageReceived(           [publish_nowait — fire-and-forget]
        account=name,                      [flat compatibility surface]
        message_id, channel_id, author_id, content, dm,
        message=<rich DTO>)                [typed Any: core never imports adapters]
        │
        ▼  EventBus.publish — subclass-aware fan-out, per-handler isolation
        ├────────────────────────────────────────────────────────────────┐
        ▼                                                                ▼
 core.account_manager.manager.                          core.plugin_manager.manager.
 AccountManager._audit_inbound                          PluginManager._on_message
        │  preview 80 chars, newlines escaped                 │  sync bus handler: spawn a
        ▼  rich note = embeds/components/                    │  fire-and-forget dispatch task
 core.audit.AuditRecorder.record(                        │  (the publisher is never blocked)
        direction="in", action="message",                 ▼
        channel_id, content=preview,                PluginManager._dispatch(event, runtimes)
        note="author=… dm… embeds=N …")                   │  for each enabled+active runtime:
        │  never raises (guarded)                          │    PluginContext built (LIVE settings dict)
        ▼                                                 ▼
 DB audit_logs row + ring + WS subscribers        runtime.instance.handle_event(ctx, event)
 (dashboard Audit page live tail)                       │  plugin filters FAST (type + trigger),
                                                        │  then reacts: e.g. SoulcopyPlugin
                                                        │  checks owner + trigger, validates the
                                                        │  channel, and submits sends (flow b)
```

Also subscribed to `MessageReceived` (not drawn):

* `core.remote_control.RemoteControl._on_message` — RETIRED from
  production (mention-prefix commands now arrive via the client's
  `ext.commands` layer into the self-command hub, gated by
  `AccessManager.can_invoke`); the bus subscriber survives only in the
  simulator stack (`control_mention` scenario).
* `core.bootstrap._wire_event_logging`'s `_on_event` — deliberately NOT
  interested: `MessageReceived` is excluded from `_LOGGED_EVENTS` as too
  chatty; lifecycle events become structured log lines instead.
* `core.account_manager.services.AccountPluginServices.wait_for_message` /
  `collect_messages` — transient per-account subscriptions a plugin's flow
  installs and always unsubscribes in `finally`.

Edit/delete/reaction variants take a parallel shape:
`on_message_edit` → `MessageEdited` (rich post-edit message),
`on_message_delete` → `MessageDeleted` (ids only), `on_reaction` →
`ReactionAdded` (stable ids + printable emoji, `raw` runtime-only, NO
message fetch). All four event types route into the same
`PluginManager._on_message` → `_dispatch` path.

Why this shape: the library object is converted ONCE at the boundary into a
frozen, project-owned DTO; everything downstream (bus, plugins, audit,
tasks, DB) is decoupled from the library; plugins never re-fetch what the
event already held; and a slow or crashing plugin cannot stall the gateway
callback because dispatch is a task.

## (b) Outbound send: plugin → wire → audit/history/events

Worked example: `ctx.services.send_message(channel_id, content)` from a
plugin (the same pipeline carries `send_dm`, `add_reaction`, `click_button`,
and remote-control replies — only the action string differs).

```
 PLUGIN CODE (e.g. plugins.soulcopy.plugin.SoulcopyPlugin._run_copy)
        │
        ▼
 core.account_manager.services.AccountPluginServices.send_message
        │  scheduler.submit(account, plugin, "message",
        │                   channel_id=…, content=…, priority=LOW)
        │  register future for task.id in TaskAckRegistry IN THE SAME
        │  event-loop step (submit is sync → no completion event can race)
        ▼
 core.task_scheduler.scheduler.TaskScheduler.submit
        │  validates args · builds Task (id, timeout_s, max_retries)
        │  push onto the ACCOUNT's heap · ensure_lane(account)
        │  emit TaskQueued
        ▼
 TaskScheduler._lane(account)            [ONE serialized worker per account:
        │                                 concurrency = 1, never two in-flight
        │                                 requests for one account]
        ▼
 TaskScheduler._run_task(task)           [attempt loop; cooperative-cancel checkpoints]
        │
        ▼
 _GrantGate.enter(priority, seq)         [global ENTRY ordering into acquire:
        │                                 (priority, seq); one loop-hop batching
        │                                 lets a late HIGH task overtake a LOW one]
        ▼
 core.rate_limit_manager.engine.RateLimitManager.acquire(action, account, plugin, priority)
        │
        │  stage 0  GlobalSendLane.enter(priority)      [only when lane enabled]
        │           — one grant at a time across EVERY account, spaced
        │             `prefixrate` s; 0 = paused (everything holds)
        │  stage 1  SERVER cooldowns — fleet-wide `_global_until` and
        │           (account, action) `_account_cooldowns` waited out first
        │           (Grant.reason = "server")
        │  stage 2  PACING on EVERY key from _pacing_keys(account, action):
        │             ("alpha", "message")        per-TASK scope — the
        │                                         account's consecutive calls
        │             ("group:grinders", "msg")   per-ACCOUNT/GROUP scope when
        │                                         the account has a sync_group
        │                                         (A → [limit] → B → [limit] → A)
        │           interval = effective(action, account, plugin)["interval"]
        │                    = MAX(all applicable policy levels, floor 1.5s)
        │           same-key waiters granted in (priority, seq) order —
        │           heap head only; waiters wake early on policy/cooldown
        │           changes; 0.25s poll backstop
        │  stage 3  JITTER — one uniform draw from the effective jitter
        │           window, slept AFTER the grant (never moves pacing clocks)
        ▼
 Grant(action, account, plugin, wait_s, jitter_s, reason)
        │  task.grant_wait_ms / grant_jitter_ms recorded; RateLimitTriggered
        │  published when wait_s > 0
        ▼
 TaskScheduler._run_task  →  sender(task)   =  the late-bound closure in
        │                                    core.bootstrap.build._sender
        ▼                                    → sender_box["send"] =
 core.account_manager.manager.AccountManager.send_hook(task)
        │  account known?  else AccountError (NO retry)
        │  status == RUNNING and client present?  else ConnectionError
        │  (scheduler-RETRYABLE — resolves when the supervisor reconnects)
        │
        │  action == "message": rt.cache.validate_channel(channel_id)
        │      → TargetGone propagates when cached state is bad (fail fast)
        │      → AccountManager._maybe_typing (opt-in typing_probability)
        │  action == "dm": resolve_dm_channel — THREE layers:
        │      1. live memory cache (DiscordCache.dm_channel_for)
        │      2. persistent api_cache row ('dm' kind, per account)
        │      3. ONE client.open_dm(user_id) → recorded in BOTH
        │      (a DM costs one open per account lifetime, not per boot;
        │       sends go through the RESOLVED channel id — never re-fetch)
        │      on TargetGone: invalidate_dm_channel (memory + DB) so the
        │      next resolution re-opens cleanly
        │  action == "reaction"/"button": validate channel; emoji/custom_id
        │      rides in task.content; adapter resolves the message on demand
        ▼
 adapters.base.DiscordClient.send_message / add_reaction / click_button
        │  RealDiscordClient: cache-first channel/message resolution,
        │  fetch only on miss; HTTP errors translated:
        │    429 → core.errors.ServerRateLimit(retry_after, global_)
        │    403 → AccessDenied   404 → TargetGone
        ▼  (SimClient: the SimWorld records history/journal instead)
 Discord wire (or the simulated world)
        │
        ▼  sender returns a DiscordMessage (or None for reaction/button)
 TaskScheduler._run_task classifies the outcome:
    ├── success ──────────────► _finish(task, completed)
    ├── ServerRateLimit ──────► limiter.report_server_limit(account, action,
    │                            retry_after, global_) → cooldown clocks set,
    │                            parked waiters woken; retry_budget = max(max_retries, 1)
    ├── ConnectionError/Timeout► retry after min(retry_backoff·2^(attempts-1), max_backoff)
    └── FlowBlocked/other ────► _finish(task, failed) with "ClassName: reason"

 TaskScheduler._finish  [EXACTLY-ONCE terminal transition]
        ├── history row → database.repositories.tasks (best-effort)
        ├── OUTBOUND AUDIT ROW → core.audit.AuditRecorder.record(
        │       direction="out", action, channel_id, content,
        │       wait_ms=grant_wait_ms, jitter_ms=grant_jitter_ms,
        │       note="plugin=… task=… attempts=…")
        │     written HERE (not in send_hook — found at integration: it
        │     would duplicate every send); CANCELLED tasks only audit when
        │     task.sender_ran (a queued-cancel never touched the API)
        ├── terminal counters + memory tails updated
        └── emit TaskCompleted | TaskFailed(error, error_kind) | TaskCancelled
                │
                ▼  on the bus
 core.account_manager.services.TaskAckRegistry
        ├── TaskCompleted → future.set_result(summary)
        ├── TaskFailed     → re-raises the FlowBlocked subclass faithfully
        │                    (error_kind maps to CacheInvalid/TargetGone/
        │                     AccessDenied/FetchRequired/FlowBlocked) else SchedulerError
        └── TaskCancelled  → SchedulerError("task … was cancelled")
                │
                ▼
 AccountPluginServices.send_message resumes → returns None to the plugin
 (documented deviation: the protocol declares DiscordMessage; the pipeline
  yields a summary string — plugins treat sends as fire-and-await-ack)
```

Downstream bus consumers of the `Task*` events (not drawn):
`TaskAckRegistry` futures, the dashboard's task-event WS push
(`interfaces.dashboard.server.DashboardServer._attach_task_events`), and the
bootstrap event-logger (`TaskCompleted`/`TaskFailed`/`TaskCancelled` are in
`_LOGGED_EVENTS`).

Why this shape: pacing is enforced BEFORE the wire (the plugin cannot
bypass it), concurrency per account is structurally 1 (the lane pops one
task at a time), retries and audit are centralized at the terminal
transition so they are exactly-once, and the plugin gets real typed errors
(`TargetGone` etc.) reconstructed across the task-ack seam so the
flow-abort contract survives.

## (c) Management: CLI / dashboard / Discord → ControlAPI → managers → DB

```
 ┌─ CLI REPL ─────────────────────────────────────────────────────┐
 │ interfaces.cli.repl.Repl.execute(line)                          │
 │   → interfaces.cli.commands.run_command(api, line)   [shlex]    │
 └────────────────────────────┬────────────────────────────────────┘
                              │
 ┌─ CLI one-shot ─────────────┼────────────────────────────────────┐
 │ main.run_app(argv) → interfaces.cli.repl.run_once(api, argv)    │
 │   → run_command                                              │
 └────────────────────────────┼────────────────────────────────────┘
                              │
 ┌─ Dashboard (any thread) ───┼───────────────────────────────────┐
 │ HTTP handler thread → DashboardServer.call(coro_partial)       │
 │   → asyncio.run_coroutine_threadsafe(...).result(10s)          │
 │   → the SAME ControlAPI coroutines on the MAIN loop            │
 │ (WebSocket pushes are read-only: stats broadcaster +           │
 │  LogService/AuditRecorder subscribers + Task* bus events)      │
 └────────────────────────────┼────────────────────────────────────┘
                              │
 ┌─ Discord chat cmd ─────────┼───────────────────────────────────┐
 │ client ext.commands → SelfCommandHub.invoke → can_invoke gate  │
 │   (per-account prefix; AccessManager.can_invoke gate)          │
 └────────────────────────────┬───────────────────────────────────┘
                              ▼
        core.api.ControlAPI   ("the ONE application surface",
                              JSON-safe in/out, strict bools via parse_bool)
                              │
        ┌──────────┬──────────┼───────────┬─────────────┬────────────┐
        ▼          ▼          ▼           ▼             ▼            ▼
  AccountManager PluginManager ProfileManager RateLimitManager TaskScheduler …
  (mutations persist FIRST, then mutate memory)   SettingsManager
                              │
                              ▼
        database.db.Database repositories (single sqlite connection + RLock)
        accounts · account_settings · account_plugins (direct assignments —
        capabilities) · profiles (retained infra) · plugins
        (metadata/states/settings) · rate_policies · audit · logs · tasks ·
        cache · kv
                              │
                              ├──► side effects for reads: none (status pages
                              │    draw from memory + repositories only)
                              └──► side effects for mutations: bus events
                                   (PluginEnabledChanged, SettingChanged, …)
                                   and log lines — surfaces never poke
                                   managers directly, so CLI/dashboard/remote
                                   cannot drift (parity by construction)
```

Worked examples, end to end:

* **`accounts start alpha`** → `ControlAPI.accounts_start("alpha")` →
  `AccountManager.start` (the full account lifecycle in
  [RUNTIME_LIFECYCLE.md](RUNTIME_LIFECYCLE.md)) → `AccountConnected` event →
  structured log line (bootstrap wire) → the REPL renders the returned
  status dict via `interfaces.cli.commands.format_accounts`.
* **`ratelimit set message 3.0 0.2 0.8`** → `ControlAPI.ratelimit_set` →
  `RateLimitManager.set_policy_values` → `_store` (floor check →
  `UnsafePolicyError` if below 1.5 s; repo persist best-effort; wake
  waiters so parked sends recompute) → the dashboard/CLI render the policy
  string.
* **Dashboard `POST /api/plugins/set-setting`** → strict field validation →
  `ControlAPI.plugins_set_setting` → `PluginManager.set_setting` →
  `SettingsManager.set_plugin_setting` (validate → persist → `SettingChanged`)
  → `runtime.settings[key]` mutated IN PLACE — the LIVE dict shared with
  `PluginContext.settings` and `AccountPluginServices.settings`, so the
  change is visible to running plugins with no restart.
* **`@alpha estop`** (from Discord, mention prefix) → the client's
  `ext.commands` layer → `SelfCommandHub.invoke` (`AccessManager`
  gate) →
  `ControlAPI.accounts_emergency_stop("alpha")` →
  `AccountManager.emergency_stop` (synchronous cancel + background stop);
  the reply string is submitted as a NORMAL-priority `message` task —
  management traffic is rate-safe by design.

Why this shape: every mutation funnels through one facade whose inputs and
outputs are JSON-safe dicts, which is what lets three very different
transports (terminal stdin, HTTP+WS threads, Discord messages) share
identical behavior, validation, and error messages.

## (d) Self-command: Discord message → registry dispatch → paced reply

```
 Discord user types "<prefix><command> [args...]"   (prefix = the
        │                                            account's admin-panel
        │                                            prefix config over the
        │                                            env default)
        ▼
 adapters.discord.client.RealDiscordClient   [ext.commands Bot,
        │  _invoke wrapper                     user_bot=True — dispatch
        │                                     receives EVERY message;
        ▼                                     ownership is OUR gate]
 allowed_invoker_async(invoker_id)
        │  AccessManager.can_invoke(account, id):
        │    admin id (list, or admin-flagged account's user id)
        │    → allow;  else per-account allowed_user_ids → allow/deny;
        │    EMPTY list denies (fail-closed); checker errors deny
        ▼
 _parse_invocation(content, name) → (args, raw)   [token-based]
        │
        ▼
 core.plugin_manager.commands.COMMAND_HUB.invoke(account, name, args,
        │                                           channel_id, ...)
        │  routes to the bound PluginManager (a broken executor can
        │  never kill the Bot's dispatch task)
        ▼
 PluginManager.execute_self_command
        ├── _find_command: FRAMEWORK_COMMANDS first (reserved), then
        │   command_entries(account) — assigned ∧ enabled ∧ collision-
        │   resolved; handlers bound to the per-(account, plugin) instance
        ├── re-gate invoker (AccessManager when wired; legacy owner-ids
        │   fallback on unwired stacks) — defense in depth
        ├── CommandContext built (services facade, logger, channel/guild,
        │   raw, invoker_id, settings_api = _ManagerSettingsApi)
        ├── spec.args? parse_command_args → ctx.parsed   [typed+validated:
        │   missing/choices/type failures raise one-line ValueErrors]
        ├── await handler(ctx, args) → reply str
        │     ValueError → "usage: <name> <usage_line>
<reason>"
        │     Exception  → "error: <name> failed internally (see logs)"
        └── _send_reply: services.send_message(channel, reply,
            priority=NORMAL) — SAME paced pipeline as plugin sends
            (flow b), capped at 1900 chars
```

Why this shape: the registry tracks the assigned ∧ enabled set live
(the hub refreshes from the same paths that re-attach plugins, and each
fresh connect re-subscribes), invocations reuse the plugin-grade
isolation/pacing, and there is deliberately NO raw client access — a
command reply is an ordinary paced, audited message.

## (e) UI extension: plugin hooks → REST → dashboard templates

```
 Dashboard load / navigation
        │
        ▼
 interfaces.dashboard.static/js/plugin-ui.js
        ├── GET /api/ui/panels  ─► ControlAPI.ui_panels()
        │                             └► PluginManager.ui_panels():
        │                                  for the FIRST instance of each
        │                                  plugin (framework-level, deduped
        │                                  by id): await instance.
        │                                  ui_panels(ctx) → UIPanel(id,
        │                                  title, icon, sections) — a
        │                                  crashing provider is skipped
        │                                  + logged (isolation)
        │                             ◄─ [{id, plugin, title, icon,
        │                                  sections: [{kind, title, data}]}]
        │   buildPluginNav(): one sidebar nav entry + panel container
        │   per UIPanel (icon = SVG path; default when empty)
        │
        └── GET /api/ui/stats?account=<sel>  ─► ControlAPI.ui_stats(account)
                                          └► PluginManager.ui_stats():
                                               for every ENABLED plugin
                                               of the account: await
                                               instance.ui_stats(ctx) →
                                               StatCard(label, value,
                                               sub, level)
                                          ◄─ [{plugin, label, value,
                                               sub, level}]
            pluginStatsHTML(): metric cards appended to the Home tab

 RENDERING SAFETY: plugins provide structured data ONLY — every value
 passes esc() inside the dashboard's own templates (plugin-ui.js), so
 plugin output is XSS-safe by construction (no HTML from plugins).
```

Poll cadence: panels load once per dashboard load; stat cards refresh
with the selected account (views.js renderHome calls loadPluginStats).

## (f) Admin access: Discord invocation → kv rows → gate decision

```
 discord.py-self delivers a message
        │
        ▼
 RealDiscordClient._dispatch_self_command
        └─► allowed_invoker_async(invoker_id)
                 │
                 ▼
        core.access_manager.AccessManager.can_invoke(account, invoker_id)
                 │
                 ├── is_admin_id(id)?
                 │      ├── kv get "access:admins"  → id in list?
                 │      └── else kv prefix "access:account:" → collect
                 │           user_id of every admin-flagged row → id in set?
                 │
                 └── (not admin) kv get "access:account:<name>"
                        → id in row.allowed_user_ids?
                        (absent row / empty list → NOBODY; fail-closed)

 WRITES (dashboard Admin tab → /api/admin/* → ControlAPI → AccessManager):
   ids/add|remove|set      → kv "access:admins"          (+ audit row)
   perms/add|remove|set    → per-account allowed_user_ids (+ audit row)
   prefix/set              → per-account prefix_mode/prefix_symbol
                             + warm_prefix_cache (the client factory's
                             build-time cache; applies on next connect)
   account-flag            → per-account is_admin (gates on the account's
                             recorded user_id — set_account_user_id at
                             connect)
   mudae-id/set            → kv "access:mudae_bot_id" (null = env fallback)

 BOOT: AccessManager(kv, credentials) → migrate_from_credentials()
 (ONE-TIME: seed access:admins from OWNER_USER_IDS when empty; after
  this the env value is ignored for authorization) → warm_prefix_cache()
 → COMMAND_HUB.access = access (adminlist/adminperms/adminprefix verbs)
```
