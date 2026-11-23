LibzentSuite — Application Architecture
=======================================

**Revision:** 4.0
**Date:** 2026-11-17
**Status:** Binding engineering contract (verified against the current
codebase; the workspace unification, the command convergence, and the
two-level data model have landed)
**Companions:** ``integration.rst`` (the detailed module contract),
``../roadmap.md`` (the active plan + decision records)

This document describes the architecture **as it exists in the
repository**. It is the product/UI-neutral engineering contract: what
owns what, which boundaries are binding, and the invariants the code
enforces. The FastAPI core API and the Flet UI wrap the same facade —
they change no ownership described here.

---

1. Purpose
----------

LibzentSuite is a multi-account Discord orchestration framework. One
process manages any number of user accounts, paces their outbound
actions to look like well-paced humans, coordinates work across them
(workspaces + chains), and exposes everything through the CLI
self-commands and the FastAPI core API (the Flet UI is the remaining
interface) — one application facade underneath.

::

    One Core API (ControlAPI — the shared facade)
          |
      +---+---+
      |       |
     CLI    FastAPI core API (landed) + Flet UI (shell landed, full
            interface per docs/plans/flet_ui_design_contract.md)

Product principles:

1. **One orchestration concept** — the workspace owns membership,
   pacing clocks, chains, and channels. (Sync groups and the fleet
   module were merged into it and deleted, 2026-11-14.)
2. Accounts are first-class resources with their own status, tasks,
   plugin state, and configuration — independent until placed in a
   workspace.
3. **Two-level configuration** — account plugin profiles own plugin
   execution; workspace presets REFERENCE profiles to coordinate
   accounts (never copy them). §4a is the contract.
4. Operational state is observable and distinguishes configured intent
   from observed runtime state.
5. Safety systems mitigate risk; they authorize nothing. The pacing
   floor, server-limit handling, and audit trails are invariants, not
   features.

2. The composition model: Core Engine → Driver
----------------------------------------------

The central architectural relationship is the owner's Core Engine →
Driver model:

::

    ┌──────────────────────────────────────────────────────────┐
    │                   CORE ENGINE (framework)                │
    │                                                          │
    │  accounts · workspace orchestration · pacing · scheduler │
    │  persistence · credentials · audit · event bus           │
    │                                                          │
    │  provides ABILITIES through the services middleware     │
    └───────────────┬──────────────────────────────────────────┘
                    │  PluginContext / PluginServices (authoritative)
                    ▼
    ┌──────────────────────────────────────────────────────────┐
    │                    DRIVERS (plugins)                     │
    │                                                          │
    │  decide WHEN an ability is used: react to events, run    │
    │  commands, emit signals, follow chains                   │
    └──────────────────────────────────────────────────────────┘

- The **Core Engine provides the ability** — every outbound action,
  state read, credential use, and signal emission is a framework
  service call that carries pacing, validation, audit, and security.
- The **plugin drives it live** — plugin cogs receive their account's
  library client DIRECTLY (the toolbox carries it at attach,
  owner-locked 2026-11-17) plus the paced facade; they *utilize* the
  engine's orchestration, when their logic decides.
- **The framework's value is orchestration + multi-account isolation**:
  pacing law across accounts, workspaces, chains, credentials, audit.
  The paced pipeline is the good path for coordinated outbound action
  (and the framework makes no self-initiated API calls); the facade's
  persistence + orchestration verbs have no client-side equivalent.
  Capability mediation as a security boundary is RETIRED.
- Core stays game-agnostic: game-specific strings and recognition
  rules belong in parser packs and plugin code, never in ``core/``.

3. System composition
---------------------

``core/bootstrap.py::build()`` is the ONE composition root. It wires
every manager, service, and adapter; modules communicate through
their contracts and injected dependencies — no service locators, no
hidden imports across boundaries, no global mutable wiring. The boot
sequence (verified order): credentials/config → database (migrations
apply under the write lock) → log service → event bus → limiter →
signal outbox → workspace service (+ pacing restore) → chain runtime
+ dispatch guard + reconciler → scheduler → plugin manager → account
manager → ControlAPI → framework handle. ``Framework.shutdown()``
reverses it fault-tolerantly: every teardown step runs even if an
earlier one failed, and each failing step is recorded on
``Framework.shutdown_failures`` (the durable witness — the log bridge
detaches mid-teardown, so post-detach warnings reach no capture).

Module map (current):

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Module
     - Owns
   * - ``core/api.py``
     - ControlAPI — the shared facade every interface calls
   * - ``core/workspace/``
     - THE orchestration: service (one writer), models, transitions, policy, chains runtime, durable signals, dispatch guard, reconciler, presets + executions (the two-level model's coordination wing)
   * - ``core/account_manager/``
     - account lifecycle, runtime client/cache, sender hook, lifecycle governor, waiter registry, the per-(account, plugin) services facade
   * - ``core/plugin_manager/`` + ``core/plugin_base.py``
     - the driver contract: discovery, per-account instances, dispatch, COG/EMITS/FOLLOWS validation, the cog host (§5)
   * - ``core/settings_manager.py``
     - the one settings schema every surface consumes
   * - ``core/rate_limit_manager/``
     - policies (global/account/plugin/pair), pacing, workspace clocks, 429s, warnings
   * - ``core/task_scheduler/``
     - queue, per-account lanes (concurrency 1), retries, cancellation, history
   * - ``core/event_bus.py``
     - typed in-process pub/sub (coalesced, bounded)
   * - ``core/access_manager.py``
     - the command gate: per-account allowlists, admin list (fail-closed)
   * - ``core/cooldowns.py``, ``core/plugin_state.py``, ``core/plugin_scopes.py``
     - plugin-facing persistence: cooldowns (profile-scoped), scoped state, channel scopes
   * - ``core/credential_service.py``, ``core/credentials.py``
     - Fernet-encrypted tokens; ids from ``.env``
   * - ``core/audit.py``, ``core/logging_service.py``
     - the audit trail and structured logs
   * - ``database/``
     - SQLite: ordered migrations (v16 current), repositories (workspaces, presets, executions, profiles, signals, channels, accounts, plugins, …)
   * - ``adapters/discord/``, ``adapters/simulator/``
     - the platform boundary: the live client (the better.py-self fork, §5a) and the complete offline world
   * - ``interfaces/cli/``
     - REPL + one-shot commands
   * - ``interfaces/api/``
     - the FastAPI core API: Pydantic models, X-API-Token auth, REST over the same facade (uvicorn serve wiring is the remaining connective item)
   * - ``interfaces/ui/``
     - the Flet shell: page registry + API client (the full interface is the active queue's #1)

4. The workspace orchestration layer
------------------------------------

``core/workspace/`` is the single owner of coordinated behavior (the
W0 spec retired 2026-11-17 — the decision record below is the
authoritative contract):

- **Membership** — ``accounts.workspace`` is THE authoritative pointer;
  the ``workspace_members`` table is a same-transaction projection never
  independently writable. A move fences the old workspace (generation
  bump) and requeues stale tasks without touching account-owned
  configuration.
- **The one writer** — ``WorkspaceService`` persists first, then
  publishes; one command at a time under its lock. Repositories own
  rows, the service owns domain transactions.
- **Chains** — the run engine executes IMMUTABLE definition snapshots
  (edits affect future runs only); routing is deterministic
  (``specific_account`` or ``first_eligible`` by seniority); every target
  passes the 7-check policy, and a denial is a **visible skip with the
  reason recorded** — never a silent drop, never a blind send.
- **Durable signals** — acceptance persists BEFORE acknowledgement;
  delivery is at-least-once with dead-lettering; consumers dedupe by
  ``signal_id``. Signals authorize nothing.
- **Transitions** — ``core/workspace/transitions.py`` is the single
  validation source; nothing else decides a legal move. Generation
  fences reject stale dispatches; the reconciler repairs drift
  idempotently and never guesses.
- **Four execution contracts, deliberately separate** (the shared
  clock is NOT a mutex): rate coordination (workspace clock), task
  concurrency (per-account lanes), chain ordering (the run engine),
  and mutation serialization (the one-writer lock). Sharing a clock
  never implies sharing execution.
- **The dispatch guard** — the scheduler's pre-dispatch gate. It
  decides WHETHER, the limiter decides WHEN; it can never weaken
  pacing. Non-member accounts are none of its business.
- **Channels** — registry (identity; validity is an observed state —
  ``unknown | available | inaccessible | deleted | stale`` — never a
  guess, and only ``available`` resolves) / account assignments /
  per-plugin bindings initialized by copy then free to diverge.
  Registry removal is REFUSED while assignments or bindings still
  reference the entry (explicit dependent removal, never silent
  cascade). A channel-bound operation verifies usability at dispatch.
- **Cross-store deletion** — account removal is a durable, retryable
  workflow (``deletion_jobs``) spanning the main DB and the separate
  plugin-data store; failures quarantine with the error surfaced.

The 18 locked workspace decisions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Locked 2026-11-13 (1–8), refined 2026-11-14 (8 revised, 9–10),
amended rev 3 (11–18). Decisions 8–10 and 15–16 are superseded by the
two-level model (§4a) — retained here for provenance.

.. list-table::
   :header-rows: 1
   :widths: 4 22 74

   * - #
     - Decision
     - Locked choice
   * - 1
     - Follow-up trigger
     - Hybrid: task-terminal bus events (mechanical) + plugin-emitted signals (semantic)
   * - 2
     - Chain definition
     - Both layers: plugins declare EMITS/FOLLOWS; workspaces bind them into named chains
   * - 3
     - Inter-step pacing
     - Shared clock only — members coordinate rate-limit stamps
   * - 4
     - Membership cardinality
     - At-most-one workspace per account
   * - 5
     - Migration
     - Big-bang: schema v10 folds groups+fleet; old tables drop in it
   * - 6
     - Fleet survivors
     - Transitions, generation fences, reconciler, policy check order carry over renamed
   * - 7
     - API surface
     - Clean replace: ``workspaces_*`` replaces ``groups_*`` + ``fleet_*``, no aliases
   * - 8
     - Config layer
     - Copy-only templates — SUPERSEDED by §4a: presets reference profiles
   * - 9
     - Preset semantics
     - Copy-on-apply + provenance — SUPERSEDED by §4a: apply activates in place
   * - 10
     - Preset portability
     - Plugin-bound; cross-plugin reuse only via validated export/import
   * - 11
     - Membership truth
     - ``accounts.workspace`` sole pointer; ``workspace_members`` a same-transaction projection
   * - 12
     - Signal delivery
     - Durably accepted BEFORE ack; at-least-once; dedupe by stable identity
   * - 13
     - Target selection
     - Explicit per-step routing: ``specific_account`` or deterministic ``first_eligible``
   * - 14
     - Definition changes
     - Runs execute immutable snapshots; edits affect future runs only
   * - 15
     - Preset versions
     - Versions immutable (retained); provenance now references profiles (§4a)
   * - 16
     - Apply concurrency
     - Atomic + revision-checked; a stale preview conflicts, never overwrites
   * - 17
     - Clock vs execution
     - Shared clock ≠ execution mutex — four separate contracts (above)
   * - 18
     - Account deletion
     - Durable, retryable cross-store workflow with observable completion

4a. The two-level data model (profiles + presets)
-------------------------------------------------

The owner-locked model (2026-11-15; implemented as P1–P8, migrations
v12–v16). **Account plugin profiles** handle independent plugin
execution; **workspace presets** coordinate multiple accounts as a
group. A preset is NOT another universal plugin-configuration system —
it references and coordinates profiles; it never replaces them.

.. list-table::
   :header-rows: 1
   :widths: 34 33 33

   * - Capability
     - Account plugin profile
     - Workspace preset
   * - Plugin configuration
     - **Owns**
     - References
   * - Channel configuration
     - **Owns**
     - References indirectly
   * - Cooldown state
     - **Owns** (profile-scoped)
     - Tracks coordination dependencies
   * - Individual execution task
     - **Owns**
     - Orchestrates
   * - Account-specific runtime state
     - **Owns**
     - Does not replace
   * - Multi-account workflow state
     - —
     - **Owns**
   * - Cross-account synchronization
     - —
     - **Owns**
   * - Execution dependencies
     - —
     - **Owns**

Binding invariants (enforced, not conventional):

- **Identity** — ``(account, plugin, profile_name)``, unique; opaque
  JSON bodies the core never interprets; cascade follows ownership.
- **Isolation by construction** — the facade's profile verbs carry
  ZERO scope parameters; a cooldown or task in one profile never
  blocks another; lifecycle changes to one profile never affect
  another; identical names across accounts/plugins never collide.
- **The active-profile pointer** — per (account, plugin), cleared
  atomically on delete; ``profile_settings()`` is the resolution seam.
- **Reference, never copy** — presets reference profiles by identity;
  apply activates in place; drift between a preset and its referenced
  profiles is DETECTED and fails closed; versions are immutable.
- **Executions** — a running/recoverable preset instance: checkpoints
  resume without replaying completed steps; cross-account
  synchronization state lives on the execution, never on the
  accounts.
- **The legacy fold (v15)** — template bindings became account-owned
  instances, idempotently; template semantics are gone.

5. The driver contract (plugins)
--------------------------------

A plugin is a ``PluginBase`` subclass discovered from a directory. It
declares: metadata, a ``Setting`` manifest, lifecycle hooks
(``on_load``/``on_enable``/``on_disable``/``on_unload``), event handling, and
optionally:

- **``COG``** — the command face: a real library Cog class (the
  2026-11-15 convergence; the manual COMMANDS grammar is retired).
  The cog host builds ONE instance per (account, plugin) — hosted
  exactly while assigned ∧ enabled (add_cog/remove_cog is the gate).
  The toolbox injected at attach carries ``to_rich``/``audit_rich`` and
  the per-(account, plugin) paced facade; the client's ``before_invoke``
  lifts it onto ``ctx.services``. Command responses ride ``ctx.send`` —
  the ONE documented library-native send; every other send goes
  through the paced pipeline. An unbound facade is ABSENT (fail-closed
  getattr), never a fallback to another account's pipeline.
- **``EMITS``** — bare signal names the plugin may emit via
  ``await ctx.services.emit_signal(name, payload)`` (namespaced
  ``<plugin>:<name>`` at consumption — no cross-plugin spoofing).
- **``FOLLOWS``** — triggers (a signal, or a terminal task event from an
  acting plugin) each naming one handler method; workspaces bind the
  declarations into named chains, and the runtime invokes handlers
  through ``PluginManager.invoke_handler`` with the per-account context.

The loader validates declarations at discovery and fails a broken one
at boot (fail-fast, the COG convention). Capability on an account is
the direct ``account_plugins`` assignment — fail-closed: no assignment,
no plugins. Do not reintroduce permission bundles.

5a. The adapter boundary (the better.py-self fork)
--------------------------------------------------

The live Discord client is the better.py-self fork, adopted fully
(the integration study's contract, implemented):

- **Lazy boundary** — imported only at ``adapters/discord/``; the fork
  package is a pinned dependency, the live client never loads in
  tests/simulation (the simulator is a complete offline world).
- **Memory-capped caches + per-account cache persistence** — restarts
  rehydrate from disk instead of re-caching cold; ``cache_stats()``
  surfaces the caps.
- **The authorization gate rides ``bot.add_check()``** — evaluated
  BEFORE argument parsing, backed by the AccessManager's per-account
  allowlists + admin list (fail-closed).
- **Dynamic callable prefixes** — per-account prefix resolution at
  invocation, not a static table.
- **``on_http_throttle`` is the real throttle signal** — the library
  absorbs 429s internally; the adapter bridges them onto the bus as
  ``RateLimitTriggered`` so the limiter earns paced retries (shared-scope
  429 correctness).
- **The durable signal outbox** — the fork's send path persists
  signal acceptance before acknowledgement (§4's durable-signals
  contract rides this).

6. Safety and data semantics (binding)
--------------------------------------

- **Pacing floor**: no configured interval below
  ``MIN_ACTION_INTERVAL = 1.5`` s (cap ``MAX_INTERVAL = 86400`` s). Server
  limits (429/Retry-After) are always respected and earn paced
  retries. The workspace policy may allow, block, or defer — never
  weaken pacing.
- **The one pipeline**: ``PluginServices`` → ``TaskScheduler.submit`` →
  one in-flight action per account → ``RateLimitManager.acquire`` →
  final validation → sender hook → adapter → audit/history/events.
  Retries only for scheduler-classified transient errors; timeouts
  are terminal (an in-flight send cannot be un-sent).
- **Snowflakes are exact strings at the data boundaries** — JSON,
  persistence, logs, any future browser state (never ``Number`` in
  JavaScript). The Python API takes ``int`` ids (the scheduler
  validates channel/user/message ids as integers); conversion
  happens at the serialization seams.
- **Credentials**: write-only surfaces, Fernet-encrypted at rest, the
  master key outside the database. Tokens never render into logs,
  responses, or exceptions.
- **Scopes compose most-restrictive-wins**: per-task
  ``(account, action)`` over per-workspace ``(workspace:<name>, action)``;
  learned per-action defaults apply when no policy is stored.

7. Events and persistence
-------------------------

- Typed events communicate observations; **an event is never
  authorization** for an operation. Durable changes go through the
  owner service, validated, persisted, then published.
- The adapter converts raw library objects into immutable DTOs ONCE at
  the boundary; runtime-only ``raw`` handles never persist.
- Repositories own persistence; services own transactions. Applied
  migrations are append-only (the runner serializes each migration
  under the write lock — parallel first boots cannot interleave).
  ``Database(":memory:")`` stays usable by tests/simulation.
- Plugin documents (``state/plugin-data.db``) keep the fixed lattice
  account → plugin → guild → channel: standalone copies, optimistic
  version writes, conflicts surfaced — never silently merged.

8. Lifecycle and testing
------------------------

- ``build()`` boots the whole stack; shutdown reverses it in order
  (scheduler drain → periodic tasks → persistence queues → plugins →
  plugin-data store → database), every step fault-tolerant.
- Tests are deterministic: injected clocks, in-memory databases, the
  simulator adapter. No real credentials or network in routine tests.
  Focused suites first, then adjacent, then the full suite (~1,680
  tests in CI: 3.10–3.13 on Linux, 3.10 and 3.13 on Windows). Never
  weaken a test to reach green.
- The project is platform-independent: no platform-only modules, and
  one suite that runs green on Linux and on native Windows — CI runs it
  on both. Tests that need an OS capability probe for it and skip with
  the reason where it is missing.

9. Recorded architecture decisions
----------------------------------

- The 18 locked workspace decisions live HERE (§4) — the W0 spec was
  absorbed and retired 2026-11-17; provenance remains in
  ``../roadmap.md`` and the originals in git history.
- The two-level data model contract (absorbed 2026-11-17, now §4a)
  and the fork adoption contract (absorbed, now §5a) live HERE — the
  plan files were retired when their contracts landed.
- ``docs/plans/`` holds planned and in-progress contracts ONLY. When a
  plan ships: the implemented structure moves here, the historical
  record moves to ``../reference/completed.rst``, and the plan file is
  retired (git history preserves the original).
