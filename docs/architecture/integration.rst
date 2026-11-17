LibzentSuite — Integration Contract
===================================

**Revision:** 3.1 (2026-11-17 — RST conversion; schema head corrected
to v16. Rev 3.0 verified against the post-unification codebase; the
workspace layer is the single orchestration owner)

1. Authority, scope, and conflict resolution
--------------------------------------------

This document binds how modules integrate TODAY. ``architecture.rst``
carries the engineering invariants and the composition model;
``../roadmap.md`` carries the decision record. When documents conflict:
the code is truth, this contract is the summary, and a divergence is a
bug in one of them — fix the divergence, do not fork the contract.

Ground rules
~~~~~~~~~~~~

- Every rule here describes current behavior. A deliberate change
  updates this file in the same series (AGENTS.md rule 8).
- Removed systems (dashboard, sync groups, fleet, Mudae parser pack)
  stay removed: do not reintroduce them or their tables.

2. Architectural boundary
-------------------------

::

    main.py -> core/bootstrap.py::build()  (the ONE composition root)
            -> ControlAPI (core/api.py — the shared facade, 105 methods)
            -> interfaces/cli (the wired interface; the FastAPI surface in
               interfaces/api/ + the Flet shell in interfaces/ui/ are
               landed-but-unwired over the same facade)

- Interfaces (CLI today, REST/WS + Flet next) parse input, call
  ControlAPI, render output. They never reimplement domain rules.
- Plugins reach the platform ONLY through ``PluginContext`` /
  ``PluginServices`` — the Core Engine → Driver model
  (architecture.rst §2). Services are the authoritative middleware.
- ``core/`` never imports adapters at runtime (event DTOs are typed
  ``Any`` on the core side); adapters never make policy decisions.

3. Current module ownership
---------------------------

.. list-table::
   :header-rows: 1
   :widths: 22 48 30

   * - Area
     - Owns
     - Must not absorb
   * - ``core/bootstrap.py``
     - dependency construction, wiring, lifecycle composition
     - domain decisions that belong in services
   * - ``core/api.py``
     - shared application operations and validation orchestration
     - HTTP/presentation details or interface-specific business logic
   * - ``core/workspace/``
     - THE orchestration: membership (pointer + projection), chains + runs, durable signals, presets, channels, cooldowns, transitions validation, the dispatch guard, the reconciler
     - Discord API requests, execution pacing, or interface concerns
   * - ``core/account_manager/``
     - account lifecycle, runtime client/cache, sender hook, services facade construction
     - workspace membership ownership or rate-policy calculation
   * - ``core/plugin_base.py``, ``core/plugin_manager/``
     - the driver contract, discovery, per-account attach/dispatch/state, self-commands, EMITS/FOLLOWS validation, ``invoke_handler``
     - direct DB access from plugins, pacing bypasses, interface state
   * - ``core/settings_manager.py``
     - schema validation and settings resolution
     - UI rendering or component state
   * - ``core/task_scheduler/``
     - task queue, per-account lanes, retries, cancellation, history lifecycle
     - choosing which member is eligible or bypassing pacing
   * - ``core/rate_limit_manager/``
     - action pacing, workspace clocks, cooldowns/server 429s, warnings
     - task ownership or interpreting game messages
   * - ``core/access_manager.py``
     - the command gate (per-account allowlists, admin list — fail-closed)
     - interface rendering of the gate
   * - ``core/event_bus.py``
     - in-process typed event delivery
     - durable state truth or authorization
   * - ``core/audit.py``, ``core/logging_service.py``
     - audit trail and structured logs
     - secret disclosure or business-state ownership
   * - ``database/``
     - schemas (v16 current), ordered migrations, repository persistence
     - interface behavior or UI transformations
   * - ``adapters/``
     - Discord/simulator boundary and DTO conversion
     - core policy decisions
   * - ``interfaces/cli/``
     - command parsing, help, presentation, ControlAPI calls
     - separate business rules

4. API boundary, external calls, and cache
------------------------------------------

- The framework makes ZERO self-initiated Discord API calls — every
  call is a plugin action through the pipeline (§5). No prefetching to
  decorate interfaces.
- The adapter converts raw library events into framework DTOs once at
  the boundary; runtime-only ``raw`` handles never persist or log.
- ``fetch_user`` and DM-channel resolution use the account-scoped cache
  (``api_cache``); DM channels resolve once and stay cached.
- Discord snowflakes remain exact digit strings across Python, JSON,
  persistence, and logs. Never a JS ``Number``.

5. Outbound task and pacing contract
------------------------------------

::

    PluginServices request
        -> TaskScheduler.submit(...)                    [queued, prioritized]
        -> one in-flight action per account (lane concurrency = 1)
        -> dispatch guard (workspace policy: allow / block + retry_at)
        -> RateLimitManager.acquire(action, account, plugin, priority)
        -> final account/cache/policy validation
        -> AccountManager sender hook
        -> DiscordClient or SimClient
        -> history + audit + event publication             [exactly-once per task]

- The scheduler retries only errors it classifies as transient; a
  policy block or validation failure fails with the reason. Timeouts
  are terminal.
- The limiter is the only timing authority. Policies compose
  most-restrictive-wins across global/account/plugin/account+plugin
  scopes; the workspace clock ``(workspace:<name>, action)`` serializes
  members; learned defaults apply when nothing is stored; the 1.5 s
  floor cannot be bypassed by anything.
- The dispatch guard decides WHETHER (workspace paused, cooldowns);
  it can never alter the rate-limit grant.

6. Event and workspace contracts
--------------------------------

``core/event_bus.py`` provides typed in-process events. **An event is
never authorization** for an operation; durable changes go through the
owner service, validated and persisted first.

Workspace (the single orchestration owner)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- ``WorkspaceService`` (``core/workspace/service.py``) is the ONE writer:
  persist-first, then publish; one command under its lock.
- ``accounts.workspace`` is THE membership pointer; ``workspace_members``
  is a same-transaction projection — never independently writable.
- ``core/workspace/transitions.py`` is the transition-validation source;
  nothing else decides a legal move. Generation fences reject stale
  dispatches.
- Chain runs execute immutable definition snapshots (edits affect
  future runs); routing is deterministic; a denied step is a VISIBLE
  SKIP with the policy's reason recorded. The scheduler guard consumes
  the same policy.
- Signals (``core/workspace/signals.py``) are durable-before-ack,
  at-least-once, deduped by ``signal_id``, dead-lettered after bounded
  retries. They authorize nothing.
- The reconciler (guard.py) is idempotent: outbox sweeps, deletion-job
  resumption, interrupted-run repair, signal retention — a healthy
  pass changes nothing.
- Account removal is the cross-store deletion workflow
  (``deletion_jobs``): primary store first, then
  ``PluginDataStore.drop_account``; failures quarantine with the error
  surfaced, never silently abandoned.
- ``resume`` semantics: a suspended workspace task requeues to QUEUED —
  label operations by their real state transition, not the method
  name.

7. Persistence and schema changes
---------------------------------

- Use repository APIs for persistent writes; plugins and interface
  handlers never open connections or issue SQL.
- Applied migrations are append-only. The runner serializes each
  migration under the write lock (parallel first boots cannot
  interleave between versions); a new schema change is a new
  migration. Current head: **v16**.
- Presets are the two-level model's coordination wing — they REFERENCE
  profiles (apply activates in place; drift fails closed) — distinct
  from AccountPluginData (``state/plugin-data.db``): operational
  documents on the fixed lattice account → plugin → guild → channel,
  standalone copies, optimistic versions, conflicts surfaced.
- Scope/permission settings are preserved on unrelated edits; a
  read-modify-write must not drop reserved fields. Validate input
  types at API boundaries; never silently coerce ids to imprecise
  numbers.

8. Plugin integration
---------------------

- Capability = direct ``account_plugins`` assignment (fail-closed).
- Declarations: ``COG`` (a discord.ext.commands.Cog subclass — the
  command face; typed params are the grammar), ``EMITS`` (bare signal
  names), ``FOLLOWS`` (triggers + handler names) — validated at
  discovery; a broken declaration fails the boot. The legacy COMMANDS
  registry retired to dump/ (2026-11-15).
- Chain handlers are invoked through ``PluginManager.invoke_handler``
  with the per-account context: paced sends, live settings. A missing
  plugin/handler raises ``PluginError`` (the runtime records a visible
  skip); handler exceptions propagate (the runtime records FAILED).
- ``ctx.services.emit_signal(name, payload)`` accepts durably BEFORE it
  acknowledges; the name is bare (namespaced at consumption).
- Plugins get framework-managed cooldowns (absolute epoch expiry),
  scoped persistent state (``kv`` namespaced ``pstate:``), and per-channel
  documents — all through the facade, never direct.

9. Testing and acceptance
-------------------------

- Deterministic: injected clocks, in-memory/temp databases, the
  simulator adapter. No real credentials or network in routine tests.
- The simulator boots the FULL production stack per run (ephemeral
  state, its own master key) — the chain runtime, outbox, and guard
  ride it, so scenarios exercise the same wiring production boots.
- Acceptance for a change: focused suite → adjacent suites → full
  suite green; docs synced in the same series (AGENTS.md rule 8);
  the knowledgebase records the change via its append-only policy.

10. Current implementation map
------------------------------

- Live client: ``adapters/discord/client.py`` (better.py-self fork,
  lazily imported; ``user_bot`` mode + a Bot-level ``_bot_invoker_gate``
  check before argument parsing; memory-capped caches + per-account
  persistence snapshots under ``state/``).
- Simulator: ``adapters/simulator/`` (world, scenarios, the runner that
  boots the ephemeral stack).
- CLI verbs: ``accounts | credentials | plugins | workspaces |
  ratelimit | cooldowns | tasks | simulation | logs | audit | estop |
  status | help | quit``.
- The framework verbs NO LONGER ride the accounts (owner call
  2026-11-17): the per-account command surface is the attached
  plugins' COGs, gated by the access manager before argument parsing.
  Framework control from chat is retired pending a replacement design;
  the CLI and the API surfaces own framework management.
- REST surface reference: ``interfaces/api/app.py`` (27 routes).
