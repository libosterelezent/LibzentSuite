# WORKSPACE_DESIGN.md — the Workspace unification spec (W0)

Status: **W0 SPEC (rev 2) — awaiting owner approval before W1 starts.**
Decision record locked by the owner 2026-11-13, refined 2026-11-14
(channel/preset data-model layering; decision 8 revised, 9–10 added).
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

## 2. Decision record (owner; 1–8 locked 2026-11-13, refined 2026-11-14)

| # | Decision | Choice |
|---|---|---|
| 1 | Follow-up trigger | Hybrid: task-terminal bus events (mechanical default) + plugin-emitted signals (semantic) |
| 2 | Chain definition | Both layers: plugins declare EMITS / FOLLOW-capabilities; workspaces bind them into named chains |
| 3 | Inter-step pacing | Shared clock only — workspace membership serializes fully (today's sync-group semantics) |
| 4 | Membership cardinality | At-most-one workspace per account (single pointer column) |
| 5 | Migration | Big-bang: one schema version (v10) folds groups+fleet; old tables drop in it |
| 6 | Fleet survivors | Transitions tables, generation fences, reconciler, policy check order carry over renamed; observations/relay + FleetTaskHandler become the chain layer |
| 7 | API surface | Clean replace: `workspaces_*` replaces `groups_*` + `fleet_*`, no aliases |
| 8 | Workspace config layer | **REVISED 2026-11-14 (was: live workspace > account > plugin settings inheritance).** Copy-only workspace layer: the workspace owns channel default *suggestions* and preset *templates*, applied by explicit copy onto accounts. Live plugin settings are strictly account-bound — no workspace (or global) settings inheritance |
| 9 | Preset semantics | Copy-on-apply + provenance: applying a preset copies values into the account's own configuration with `source_preset_id`/`source_version`/`applied_at`; updates are explicit (never silent overwrites); detach preserves the account's values. Replaces today's live-bound profiles (`account_plugins.profile` resolution layer) |
| 10 | Preset portability | Plugin-bound by default (schema validation is per-plugin); cross-plugin reuse only via explicit export/import, re-validated against the destination plugin's schema |

## 3. Architecture rules (owner, 2026-11-14)

1. Channel assignments are account-owned; workspace channels are
   organizational defaults, not shared runtime assignments.
2. Plugin channel bindings are account-plugin-owned.
3. Presets are templates that are copied/applied, not live inherited
   configurations.
4. Plugin settings and plugin data are independently owned by each
   account.
5. Preset updates never silently overwrite existing account
   configurations.
6. Cross-account copying or synchronization must be an explicit
   operation with per-account validation and results.
7. Workspaces coordinate operations; they never automatically merge
   account configuration or plugin data.

## 4. Data model — the layer separation

Four distinct concepts, never conflated:

| Layer | Answers | Owned by |
|---|---|---|
| **Channel assignment/binding** | WHERE an account or plugin operates | account / account-plugin |
| **Preset (template)** | WHAT starting configuration an operation uses | global / workspace / account (plugin-bound) |
| **Plugin configuration** | WHAT the account has chosen | account-plugin (strictly) |
| **Plugin data** | WHAT the plugin produced/maintains | account-plugin (→ guild → channel, opt-in) |

## 5. Channel model

Three levels, ownership flowing downward by **copy**, never live:

| Scope | Responsibility |
|---|---|
| Global | Default channel policies and available channel-selection rules |
| Workspace | Organize channels into workspace groups; MAY provide suggested defaults |
| Account | Owns the actual channel assignments used for execution |

Applying a workspace's suggested set **creates account-level
assignments** (a copy) — it must never create a live shared channel
binding. Account A and Account B in the same workspace operate in
entirely different guilds/channels by default.

### 5.1 Storage: three explicit tables

```
account_channels            (registry)  account, channel_id, guild_id, name
account_channel_assignments             account, channel_id, added_at
account_plugin_channels     (bindings)  account, plugin, channel_id
```

- **Registry** — channels known/available to the account (mirrors
  what the cache sees; persisted so configuration survives restarts).
- **Assignments** — the account-level selection used for operations.
- **Bindings** — the channels ONE plugin uses on ONE account. A
  plugin initializes its bindings from the account's assignments at
  assign time (copy), then diverges freely. A plugin never silently
  inherits later account-assignment changes.

Migrates FROM: the reserved `"channels"` key inside profile settings
(`core/plugin_scopes.py` convention) — extracted into real rows.

## 6. Preset model

### 6.1 Three template scopes (all plugin-bound)

| Scope | Purpose | Owner |
|---|---|---|
| Global | Reusable baseline templates | application |
| Workspace | Templates matching a workspace's conventions | workspace |
| Account | The account's own reusable templates | account |

### 6.2 Lifecycle

```
create → validate → apply → customize → update (explicit) → detach
```

- **validate** — against the target plugin's Setting schema AND
  schema version (ControlAPI layer, never the repository — existing
  rule).
- **apply** — copies values into the account's plugin configuration
  (the `plugin_settings` layer); records provenance on the
  account-plugin pair: `source_preset_id`, `source_version`,
  `applied_at`.
- **customize** — subsequent edits touch the account's values only.
- **update** — applying a newer preset version is an explicit
  operation with a diff preview; never an automatic overwrite; local
  customizations are surfaced before confirming.
- **detach** — drops the provenance link; the account keeps its
  current values; the source preset may change or be deleted freely.

### 6.3 Resolution chain (SIMPLIFIED from today)

```
today:    defaults ← global ← bound profile ← account overrides   (live)
v10:      defaults ← global ← account values                      (copy)
```

The profile layer disappears from live resolution — presets seed the
account layer at apply time. One fewer layer, no cross-account
surprise propagation, and `set_setting`'s live in-place update
behavior is unchanged.

### 6.4 Cross-plugin reuse

Preset records carry their plugin binding (`UNIQUE(plugin, name)`
today — kept). **Export/import** is the only cross-plugin path:
export renders the template's validated values; import re-validates
against the destination plugin's schema and creates a NEW preset
bound to that plugin. No silent schema-compatibility guessing.

## 7. Ownership lattice

| Resource | Global | Workspace | Account | Account + Plugin |
|---|---|---|---|---|
| Channel defaults | define | optional suggestions (copy) | own assignments | own bindings |
| Data presets | own templates | own templates | own templates | plugin-specific templates |
| Plugin settings | catalog/policies only | **no shared settings** | — | own settings |
| Plugin runtime data | no shared account data | no shared account data | — | own data |
| Plugin credentials | no shared credentials | no shared credentials | — | own credentials |

**Ownership key** (conceptual): `workspace → account → plugin
(→ guild → channel, only where a plugin genuinely needs that scope)`.
Implementation reading, fixed by decision 4: account identity stays
globally unique (at-most-one workspace), so rows key by account name
as today — the workspace prefix expresses membership context and the
**deletion boundary**, not a re-scoping of account identity. The
guild/channel levels remain opt-in (the `"0"` sentinel convention
carries over).

**Deletion boundary (account removal):** everything keyed under the
account dies, across BOTH stores — assignments, settings, states,
kv rows, cooldowns, channel registry/assignments/bindings, presets
the account owns, **and plugin-data documents** (the store gains a
`drop_account`; today it has none — the inheritance gap found
2026-11-13). Orphaned `plugin_settings`/`plugin_states` rows after
removal are likewise gone under this rule.

## 8. Entity model

```
┌──────────────────────────────┐
│ workspaces                   │
│──────────────────────────────│
│ name (UNIQUE)   description  │
│ enabled         paused       │
│ generation      created_at   │
│                updated_at    │
└──────┬───────────────────────┘
       │ 1..*        (at-most-one per account — decision 4)
       ▼
┌──────────────────────────────┐     ┌──────────────────────────────┐
│ workspace_members            │     │ accounts                     │
│──────────────────────────────│     │──────────────────────────────│
│ account (UNIQUE)             │────►│ name (PK)   workspace        │
│ workspace   mode             │     │ enabled …                    │
│ eligibility  joined_at       │     └──────┬───────────────────────┘
└──────────────────────────────┘            │
       ┌────────────────────┬──────────────┼─────────────────────┐
       ▼                    ▼              ▼                     ▼
┌─────────────────┐ ┌───────────────┐ ┌──────────────────┐ ┌──────────────┐
│ account_channels│ │ account_      │ │ account_plugins  │ │ presets      │
│ (registry)      │ │ channel_      │ │──────────────────│ │──────────────│
│ account,chan,gld│ │ assignments   │ │ account, plugin  │ │ scope(global/│
└─────────────────┘ └───────────────┘ │ source_preset_id │ │ workspace/   │
       ┌─────────────────┐            │ source_version   │ │ account),    │
       │ account_plugin_ │            │ applied_at       │ │ plugin, name │
       │ channels        │◄───────────┤ (assignment +    │ │ version,     │
       │ (bindings)      │            │  provenance)     │ │ settings_json│
       └─────────────────┘            └──────────────────┘ └──────────────┘

┌──────────────────────────────┐   ┌──────────────────────────────┐
│ workspace_chains             │   │ workspace_tasks              │
│──────────────────────────────│   │──────────────────────────────│
│ id, workspace, name (UNIQUE) │──►│ chain_id, step, status       │
│ enabled, definition_json     │   │ account, task_id, generation │
└──────────────────────────────┘   │ error, timestamps            │
┌──────────────────────────────┐   └──────────────────────────────┘
│ workspace_cooldowns          │   ┌──────────────────────────────┐
│ scope(workspace|member)      │   │ workspace_transitions        │
│ reason, window, source       │   │ (history, unchanged shape)   │
└──────────────────────────────┘   └──────────────────────────────┘
```

Carried vocabulary (from `core/fleet/models.py`, renamed):
`MemberEligibility`, `ExecutionMode`, `AssignmentStatus` (+ step
states), `CooldownScopeType` (workspace/member), `CooldownSource`.

## 9. Trigger catalog (decision 1: hybrid)

### 9.1 Mechanical triggers — task-terminal events (exist today)

| Event | Fields today | Gap |
|---|---|---|
| `TaskCompleted` | account, task_id, duration_s, summary | **no `plugin`** |
| `TaskFailed` | account, task_id, error, error_kind | **no `plugin`** |
| `TaskCancelled` | account, task_id | (not a chain trigger by default) |

**W4 prerequisite:** the scheduler already holds `task.plugin` at
emit time (`scheduler.py:1618`) — extend `TaskCompleted`/`TaskFailed`
with an additive `plugin: str = ""` field.

### 9.2 Semantic triggers — plugin-emitted signals (new)

```python
async def emit_signal(self, name: str, payload: dict | None = None) -> None
```

- Fire-and-forget as a `WorkspaceSignal(account, plugin, name,
  payload)` event (control-plane class — never dropped under queue
  pressure).
- Names are plugin-namespaced at consumption (`<plugin>:<name>`) —
  no cross-plugin spoofing.
- Signals AUTHORIZE NOTHING (fleet's §3 rule carries over).

## 10. Chain-binding model (decision 2: both layers)

### 10.1 Plugin declarations (static, on PluginBase)

```python
class MyPlugin(PluginBase):
    EMITS: ClassVar[tuple[str, ...]] = ("claim_done", "queue_drained")
    FOLLOWS: ClassVar[tuple[FollowSpec, ...]] = (
        FollowSpec(signal="grinder:claim_done", handler="on_claim_done"),
        FollowSpec(task_action="message", handler="on_sender_message"),
    )
```

Validated at load time: hook names must exist; unknown signal
namespaces are load-warnings, not errors.

### 10.2 Workspace bindings (persisted, `workspace_chains`)

```json
{ "name": "claim-cleanup",
  "steps": [
    {"position": 1, "plugin": "grinder", "action": "message",
     "on": {"event": "task_completed", "action": "message"}},
    {"position": 2, "plugin": "cleaner", "follows": "grinder:claim_done",
     "emit": true, "gate": {"setting": "auto_clean", "equals": true}}
  ] }
```

- A step routes to ANY eligible member running the step's plugin.
- `gate` is the per-step settings condition (resolution per §6.3).
- Bindings referencing plugins a member lacks are visible skips.

### 10.3 Step execution semantics

```
PENDING → READY → RUNNING → DONE
                ↘ SKIPPED (plugin absent | gate denies | no eligible member)
                ↘ FAILED (attempts exhausted; retry policy per chain)
```

SKIPPED is recorded with a reason; `on_skip: "continue" | "abort"`
is per-chain.

## 11. Pacing (decision 3: shared clock only)

`RateLimitManager.set_account_group` semantics carry verbatim under
a workspace rename: membership registers on the `workspace:<name>`
shared clock; the last member leaving drops the stamps. Chained
steps pace on the same clock. No step-gap knob in v1.

## 12. Eligibility gate (W1 policy — fleet's check order, extended)

1. framework safety (estop / scheduler halted)
2. workspace enabled ∧ not paused (generation current)
3. account connected ∧ member eligibility permits
4. workspace cooldowns permit (most-restrictive wins)
5. task assignment valid (generation match — the fence)
6. prerequisites met (chain position ordering)
7. **NEW: step's plugin assigned ∧ enabled ∧ chain gate passes —
   including a channel binding that resolves (§5.1) for the
   step's target**

Check 7 is the "if settings/plugins allows it" of the goal, plus
the WHERE: a step with no usable channel binding on the target
account is a visible SKIPPED, not a dead send.

## 13. Migration mapping (decision 5: big-bang, schema v10)

Current schema: **v9**.

| v9 table | → v10 |
|---|---|
| `sync_groups` | `workspaces` (name-keyed; generation/paused folded from `fleet_groups`) |
| `accounts.sync_group` | `accounts.workspace` (renamed pointer) |
| `fleet_groups` | folded INTO `workspaces`; dropped |
| `fleet_members` | `workspace_members` (account UNIQUE — decision 4) |
| `fleet_assignments` | `workspace_tasks` |
| `fleet_cooldowns` | `workspace_cooldowns` (scope rename) |
| `fleet_transitions` | `workspace_transitions` |
| `plugin_profiles` | `presets` (scope=global; **+ version column** for update detection) |
| `account_plugins.profile` (live binding) | **materialized**: for every bound pair, each profile key not overridden by the account layer is copied INTO `plugin_settings`; provenance set (`source_preset_id`, `source_version=1`, `applied_at=migration`); the `profile` column drops. Effective settings are preserved byte-for-byte |
| reserved `"channels"` key in profile settings | extracted into `account_plugin_channels` bindings |
| — (new) | `account_channels`, `account_channel_assignments`, `account_plugin_channels`, workspace-scope `presets` |
| — (cross-store) | `PluginDataStore.drop_account(account)` added; account removal calls it |

Migration test (W2 exit): a populated v9 fixture DB lands in the
workspace shape with memberships, generations, cooldowns, effective
settings (post-materialization) and channel bindings intact.

## 14. API surface (decision 7: clean replace)

**ControlAPI** (replaces 9 `groups_*` + 3 `fleet_*`):

```
workspaces_list/get/create/update/delete
workspaces_add_account / remove_account / set_account
workspaces_chains_list / chain_get / chain_set
workspaces_tasks / workspaces_op(op)

# channel + preset surface (new; accounts_* family)
accounts_channels_list / channel_add / channel_remove / channel_set
accounts_plugin_channels_get / set
presets_list / get / create / update / delete / apply / detach
presets_export / import
```

**CLI**: `workspaces …` replaces `groups`/`fleet`; `accounts …
channels …`, `presets …` verb groups added.
**Simulator**: `control_mention` re-targets workspace verbs; fleet
scenarios carry over renamed.

## 15. UI navigation (future Flet surface — reference model)

| Area | Manages |
|---|---|
| Global → Channels | channel policies and defaults |
| Workspace → Channels | workspace channel organization + suggested defaults |
| Account → Channels | the account's actual channel assignments |
| Account → Data Presets | account-owned reusable presets |
| Account → Plugins | installed/enabled plugins, independent configurations |
| Account → Plugins → Plugin | settings, channel bindings, presets, data for that pair |
| Extensions → Preset Catalog | discoverable templates + plugin-provided preset definitions |

A workspace-level view may AGGREGATE channel assignments and plugin
setup across accounts, but editing always resolves to an explicit
account context.

## 16. Carry-over vs new surface

| Carries over (renamed, tests carry) | New (built + tested fresh) |
|---|---|
| transitions tables + validators | chain runtime (W4) |
| generation fences | `WorkspaceSignal` + `emit_signal` API |
| reconciler 30 s idempotent passes | channel registry/assignments/bindings |
| policy check order (§12) | preset lifecycle: apply/update/detach + provenance |
| single-lock persist-first writer discipline | preset export/import |
| shared-clock engine semantics | live-binding materialization migration |
| cooldown records (scope-renamed) | `PluginDataStore.drop_account` + removal cascade fix |
| task-terminal `plugin` field extension | workspace-scope presets |

## 17. Resolved during W0 (recorded for W1)

- Decision 8 revised + 9/10 added 2026-11-14 (this rev): copy-only
  workspace layer; copy-on-apply presets; plugin-bound presets with
  export/import. Today's live-bound profiles END at v10.
- `TaskCompleted`/`TaskFailed` lack `plugin` — additive field lands
  in W4's first commit.
- Multi-workspace membership stays out (decision 4).
- Workspace rate-policy LEVEL: out of scope v1 (pacing is
  membership-driven, decision 3).
- `capability.py` (`FleetTaskHandler`) deleted in W5 with its 33
  tests; EMITS/FOLLOWS coverage replaces them.
- Account removal deletion boundary (§7) absorbs both 2026-11-13
  cascade gaps (plugin-data inheritance; orphaned
  plugin_settings/plugin_states).
