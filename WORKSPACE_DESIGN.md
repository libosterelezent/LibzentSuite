# WORKSPACE_DESIGN.md — the Workspace unification spec (W0)

Status: **W0 SPEC — awaiting owner approval before W1 starts.**
Decision record locked by the owner 2026-11-13 (interactive session);
this document grounds those decisions in the codebase as it exists.
Companions: `roadmap.md` (phases W0–W5),
`knowledgebase/WORKSPACE_SUPERSESSION.md` (append-only record).

---

## 1. Goal

Replace `core/sync_group_manager/` + `core/fleet/` — two modules, one
named entity with two owners, closure-wired in bootstrap — with ONE
concept:

```
Workspace → Multiple Accounts → Plugin assignment → Orchestration
```

Accounts stay independent (per-account plugin assignment via
`account_plugins`, unchanged) until added to a Workspace. The
workspace owns membership, orchestration state, and coordinated
chains: *account A does something, follow-up account B cleans it up —
if settings/plugins allow it.*

## 2. Locked decision record (owner, 2026-11-13)

| # | Decision | Choice |
|---|---|---|
| 1 | Follow-up trigger | Hybrid: task-terminal bus events (mechanical default) + plugin-emitted signals (semantic) |
| 2 | Chain definition | Both layers: plugins declare EMITS / FOLLOW-capabilities; workspaces bind them into named chains |
| 3 | Inter-step pacing | Shared clock only — workspace membership serializes fully (today's sync-group semantics) |
| 4 | Membership cardinality | At-most-one workspace per account (single pointer column) |
| 5 | Migration | Big-bang: one schema version (v10) folds groups+fleet; old tables drop in it |
| 6 | Fleet survivors | Transitions tables, generation fences, reconciler, policy check order carry over renamed; observations/relay + FleetTaskHandler become the chain layer |
| 7 | API surface | Clean replace: `workspaces_*` replaces `groups_*` + `fleet_*`, no aliases |
| 8 | Config layer | Workspace settings layer: workspace > account > plugin precedence for member plugin settings |

## 3. Entity model

```
                    ┌──────────────────────────────┐
                    │ workspaces                   │
                    │──────────────────────────────│
                    │ name (UNIQUE)   description  │
                    │ enabled         paused       │
                    │ generation      created_at   │
                    │                updated_at    │
                    └──────┬───────────────────────┘
                           │ 1..*            (membership is a column
                           ▼                  on the account — at-most-one)
┌──────────────────┐   ┌──────────────────────────────┐
│ accounts         │   │ workspace_members            │
│──────────────────│   │──────────────────────────────│
│ name (PK)        │◄──│ account (UNIQUE — decision 4)│
│ workspace ───────┼──►│ workspace (FK)               │
│ enabled, ...     │   │ mode: independent|managed    │
└──────────────────┘   │ eligibility: eligible|       │
                       │   waiting|paused|cooling|    │
                       │   retired                    │
                       │ joined_at   updated_at       │
                       └──────────────────────────────┘

┌──────────────────────────────┐   ┌──────────────────────────────┐
│ workspace_chains             │   │ workspace_tasks              │
│──────────────────────────────│   │──────────────────────────────│
│ id   workspace (FK)          │   │ id   workspace (FK)          │
│ name (UNIQUE per workspace)  │◄──│ chain_id (NULL = free task)  │
│ enabled   definition_json    │──►│ step: int (chain position)   │
│ created_at updated_at        │   │ status: queued|assigned|     │
└──────────────────────────────┘   │   running|completed|suspended│
                                   │   |failed|cancelled  (+ step │
┌──────────────────────────────┐   │   states: pending|skipped)   │
│ workspace_settings           │   │ account (current owner)      │
│──────────────────────────────│   │ task_id → task_history       │
│ workspace (FK)               │   │ generation (fence)           │
│ plugin   key   value_json    │   │ error   created/updated_at   │
│ PK (workspace, plugin, key)  │   └──────────────────────────────┘
└──────────────────────────────┘   ┌──────────────────────────────┐
                                   │ workspace_cooldowns          │
┌──────────────────────────────┐   │──────────────────────────────│
│ workspace_transitions        │   │ scope: workspace|member      │
│──────────────────────────────│   │ scope_id   reason            │
│ id   kind   workspace        │   │ started_at expires_at        │
│ generation reason occurred_at│   │ source: manual|observed|     │
│ correlation_id                │   │   server   cleared_at       │
│ task_id  assignment_id  acct  │   └──────────────────────────────┘
└──────────────────────────────┘
```

Carried vocabulary (from `core/fleet/models.py`, renamed):
`MemberEligibility`, `ExecutionMode` (independent/managed),
`AssignmentStatus` (+ new step states), `CooldownScopeType`
(group/account → workspace/member), `CooldownSource`.

## 4. Trigger catalog (decision 1: hybrid)

### 4.1 Mechanical triggers — task-terminal events (exist today)

| Event | Fields today | Gap |
|---|---|---|
| `TaskCompleted` | account, task_id, duration_s, summary | **no `plugin`** |
| `TaskFailed` | account, task_id, error, error_kind | **no `plugin`** |
| `TaskCancelled` | account, task_id | (not a chain trigger by default) |

**W4 prerequisite:** the scheduler already holds `task.plugin` at emit
time (`scheduler.py:1618`) — extend `TaskCompleted`/`TaskFailed` with
an additive `plugin: str = ""` field so the orchestrator can match a
terminal event to the step's plugin without a task_history round-trip.
Small, backward-compatible (dataclass default), tested in W4.

### 4.2 Semantic triggers — plugin-emitted signals (new)

New facade method on `AccountPluginServices` (the approved plugin API
surface, alongside `send_message`/`cooldown_*`/`state_*`/`data_*`):

```python
async def emit_signal(self, name: str, payload: dict | None = None) -> None
```

- Fire-and-forget onto the EventBus as a `WorkspaceSignal(account,
  plugin, name, payload)` event (new, control-plane class so it never
  drops under queue pressure).
- `name` is plugin-namespaced at consumption (`<plugin>:<name>`) —
  plugins cannot spoof another plugin's signal.
- Signals AUTHORIZE NOTHING (fleet's §3 rule carries over): they are
  observations the orchestrator matches against chain bindings.

## 5. Chain-binding model (decision 2: both layers)

### 5.1 Plugin declarations (static, on PluginBase)

```python
class MyPlugin(PluginBase):
    # replaces fleet_handler (ClassVar, default absent = chain-inert)
    EMITS: ClassVar[tuple[str, ...]] = ("claim_done", "queue_drained")
    FOLLOWS: ClassVar[tuple[FollowSpec, ...]] = (
        FollowSpec(signal="mudae_like:claim_done",      # semantic
                   handler="on_claim_done"),            # hook name
        FollowSpec(task_action="message",               # mechanical
                   handler="on_sender_message"),
    )
```

Validation at load time (plugin manager): hook names must exist on
the class; unknown signal namespaces are load-warnings, not errors.

### 5.2 Workspace bindings (persisted, `workspace_chains`)

```json
{ "name": "claim-cleanup",
  "steps": [
    {"position": 1, "plugin": "grinder",  "action": "message",
     "on": {"event": "task_completed", "action": "message"}},
    {"position": 2, "plugin": "cleaner",  "follows": "grinder:claim_done",
     "emit": true, "gate": {"setting": "auto_clean", "equals": true}}
  ] }
```

- A step routes to: ANY eligible member running the step's plugin
  (single-member workspaces degrade to direct handoff).
- `gate` is the per-step settings condition (decision 8 surface).
- Bindings referencing plugins a member doesn't have are visible
  skips, never silent drops.

### 5.3 Step execution semantics

```
PENDING → READY → RUNNING → DONE
                ↘ SKIPPED (plugin absent | gate denies | no eligible member)
                ↘ FAILED (attempts exhausted; retry policy per chain)
```

SKIPPED is *recorded with a reason* and does NOT break the chain by
default (configurable per chain: `on_skip: "continue" | "abort"`).

## 6. Settings precedence (decision 8)

Resolution for a member plugin setting (most specific wins):

```
workspace_settings (workspace, plugin, key)      ← NEW
  ▸ account plugin override (plugin_settings)
    ▸ bound profile (plugin_profiles via account_plugins.profile)
      ▸ plugin defaults (Setting schema)
```

- Applied by extending the existing live-settings resolution in the
  plugin manager (`live_settings` / effective-settings path) — one
  resolution function, no per-surface copies.
- Validation unchanged: workspace settings are validated against the
  same plugin Setting schema at write time (ControlAPI layer, never
  the repository — existing rule).
- Rate policies keep their own lattice (`POLICY_LEVELS = global,
  account, plugin, account_plugin`) — a workspace rate level is
  explicitly OUT of scope for v1 (the shared pacing clock of decision
  3 is membership-driven, not policy-driven).

## 7. Pacing (decision 3: shared clock only)

`RateLimitManager.set_account_group` semantics carry over verbatim
under a workspace rename: membership registers the account on the
`workspace:<name>` shared clock; every member's grant stamps it; the
last member leaving drops the stamps (fresh clock on re-create).
Chained steps pace on the same clock — B's follow-up waits its full
interval after A's send. No step-gap knob in v1 (decision 3).

## 8. Eligibility gate (W1 policy — fleet's check order, extended)

Order (fleet `POLICY_CHECK_ORDER` + the one new coupling):

1. framework safety (estop / scheduler halted)
2. workspace enabled ∧ not paused (generation current)
3. account connected ∧ member eligibility permits (not paused/cooling/retired)
4. workspace cooldowns permit (most-restrictive wins)
5. task assignment valid (generation match — the fence)
6. prerequisites met (chain position ordering)
7. **NEW: step's plugin assigned (`account_plugins`) ∧ enabled
   (`plugin_states`) ∧ chain gate passes (§6 resolution)**

Check 7 is the "if settings/plugins allows it" of the goal — fleet
never knew plugins; the workspace policy is the single place this
coupling exists.

## 9. Migration mapping (decision 5: big-bang, schema v10)

Current schema is at **v9** (`database.schema.latest_version()`).

| v9 table | → v10 |
|---|---|
| `sync_groups` (id, name, description, enabled) | `workspaces` (name-keyed; enabled carried; generation/paused folded from `fleet_groups`) |
| `accounts.sync_group` (name pointer) | `accounts.workspace` (same shape, renamed; NULL = independent) |
| `fleet_groups` (group_name, generation, paused) | folded INTO `workspaces` columns; table dropped |
| `fleet_members` (account, group_name, mode, eligibility, …) | `workspace_members` (account becomes UNIQUE — decision 4); rows without a sync-group membership were mode-only and dissolve |
| `fleet_assignments` | `workspace_tasks` (chain_id/step NULL for free tasks) |
| `fleet_cooldowns` | `workspace_cooldowns` (scope rename) |
| `fleet_transitions` | `workspace_transitions` |

One migration version applies: create workspace tables → backfill
from sync_groups ∪ fleet_groups (join on name) → rewrite
`accounts.sync_group` → drop the five fleet tables + `sync_groups`.
Migration test (W2 exit): a populated v9 fixture DB lands in the
workspace shape with memberships, generations and cooldowns intact.

## 10. API surface (decision 7: clean replace)

**ControlAPI** (replaces 9 `groups_*` + 3 `fleet_*` methods):

```
workspaces_list / workspaces_get / workspaces_create / workspaces_update
workspaces_delete (empty-or-detach, like groups_delete)
workspaces_add_account / workspaces_remove_account / workspaces_set_account
workspaces_chains_list / workspaces_chain_get / workspaces_chain_set
workspaces_tasks            (was fleet_groups/fleet_group views)
workspaces_op(op)           (explicit op table: pause/resume/suspend/
                             requeue/handoff/withdraw/cancel/estop/
                             cooldown apply|clear/eligibility)
```

**CLI**: `workspaces …` verb group replaces `groups` and `fleet`.
**Simulator**: `control_mention` scenario re-targets the workspace
verbs; the fleet scenarios carry over renamed.

## 11. Carry-over vs new surface

| Carries over (renamed, tests carry) | New (built + tested fresh) |
|---|---|
| transitions tables + validators | `core/workspace/` chain runtime (W4) |
| generation fences | `WorkspaceSignal` event + `emit_signal` facade API |
| reconciler 30 s idempotent passes | workspace settings layer + precedence matrix |
| policy check order (§8, minus the split) | plugin-assigned∧enabled∧gate check |
| single-lock persist-first writer discipline | `workspace_chains` binding CRUD |
| shared-clock engine semantics (§7) | step SKIP semantics + `on_skip` policy |
| cooldown records (scope-renamed) | task-terminal `plugin` field extension |

## 12. Resolved during W0 (recorded for W1)

- `TaskCompleted`/`TaskFailed` lack `plugin` — additive field lands in
  W4's first commit, not W1 (the domain models accept it as a
  documented gap until then).
- Multi-workspace membership stays out (decision 4); the pointer
  column makes a later loosening a table-level change, not a rewrite.
- Workspace rate-policy LEVEL: out of scope v1 (§6).
- `capability.py` (`FleetTaskHandler`) is deleted in W5, not migrated;
  its 33 tests retire with it — EMITS/FOLLOWS coverage replaces them.
