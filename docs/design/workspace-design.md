# WORKSPACE_DESIGN.md — the Workspace unification spec (W0)

Status: **W0 SPEC (rev 3) — all 18 decisions locked; W1 may start on
owner go.** Locked 2026-11-13 (1–8), refined 2026-11-14 (8 revised,
9–10), amended 2026-11-14 from the rev 3 review round (11–18 + four
owner calls: pointer-canonical membership, preset-keys-only apply,
single-source provenance, archive on workspace delete). Review input
preserved verbatim: `docs/plans/WORKSPACE_DESIGN_W0_REV3_AMENDMENTS.md`.
Companions: `../roadmap.md`, `knowledgebase/WORKSPACE_SUPERSESSION.md`.

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

## 2. Decision record (owner; 1–8: 2026-11-13, 9–10: 2026-11-14, 11–18: 2026-11-14 rev 3)

| # | Decision | Locked choice |
|---|---|---|
| 1 | Follow-up trigger | Hybrid: task-terminal bus events (mechanical default) + plugin-emitted signals (semantic) |
| 2 | Chain definition | Both layers: plugins declare EMITS / FOLLOW-capabilities; workspaces bind them into named chains |
| 3 | Inter-step pacing | Shared clock only — members coordinate rate-limit stamps; the four execution contracts are separated (§11, decision 17) |
| 4 | Membership cardinality | At-most-one workspace per account |
| 5 | Migration | Big-bang: schema v10 folds groups+fleet; old tables drop in it |
| 6 | Fleet survivors | Transitions tables, generation fences, reconciler, policy check order carry over renamed; observations/relay + FleetTaskHandler become the chain layer |
| 7 | API surface | Clean replace: `workspaces_*` replaces `groups_*` + `fleet_*`, no aliases |
| 8 | Workspace config layer | REVISED 2026-11-14: copy-only — channel default suggestions + preset templates applied by explicit copy. Live plugin settings strictly account-bound; no workspace/global settings inheritance |
| 9 | Preset semantics | Copy-on-apply + provenance: apply copies values into the account's own config; updates explicit; detach preserves values. Live-bound profiles end at v10 |
| 10 | Preset portability | Plugin-bound by default; cross-plugin reuse only via explicit export/import re-validated against the destination schema |
| 11 | Membership source of truth | **`accounts.workspace` is the sole authoritative pointer.** `workspace_members` is a derived projection updated in the same transaction, never independently writable, reconciler-checked |
| 12 | Signal delivery | Chain-triggering signals are **durably accepted before acknowledgement** (signal row or outbox persisted first), delivered **at least once**; consumers deduplicate by stable signal identity and are idempotent |
| 13 | Chain target selection | Explicit per-step routing policy; v1 supports `specific_account` and deterministic `first_eligible`. Fan-out (`all_eligible`) deferred until partial-success/retry/cancel semantics exist |
| 14 | Chain definition changes | Each run executes against an **immutable validated definition snapshot** captured at run creation; edits affect future runs only |
| 15 | Preset versions | Preset versions are **immutable**. Updating creates a new version under the same preset identity; applied configs retain `source_preset_id` + `source_version` + copied values |
| 16 | Preset apply concurrency | Apply/update is **atomic and revision-checked**: a stale preview cannot overwrite intervening account edits — conflict, refresh, re-confirm |
| 17 | Clock vs execution | Shared clock ≠ execution mutex. Rate coordination, task concurrency, chain ordering, and mutation serialization are four separate contracts (§11) |
| 18 | Account deletion | Cross-store cleanup is a **durable, retryable workflow with observable completion** — not a single-transaction assumption across stores |

## 3. Architecture rules

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

Membership changes never delete or silently rewrite account-owned
configuration (§5 note): leaving a workspace keeps plugin settings,
plugin data, channel assignments, and plugin channel bindings.

## 4. Data model — the layer separation

| Layer | Answers | Owned by |
|---|---|---|
| Channel assignment/binding | WHERE an account or plugin operates | account / account-plugin |
| Preset (template) | WHAT starting configuration | global / workspace / account (plugin-bound) |
| Plugin configuration | WHAT the account chose | account-plugin (strictly) |
| Plugin data | WHAT the plugin maintains | account-plugin (→ guild → channel, opt-in) |

## 5. Channel model

Three levels, ownership flowing downward by **copy**, never live:

| Scope | Responsibility |
|---|---|
| Global | channel policies; available channel-selection rules |
| Workspace | organizes channels into groups; MAY suggest defaults |
| Account | owns the actual assignments used for execution |

Applying a workspace's suggested set **creates account-level
assignments** (a copy) — never a live shared binding.

### 5.1 Storage: three explicit tables

```
account_channels            (registry)     PK (account, guild_id, channel_id)
account_channel_assignments                PK (account, channel_id)
account_plugin_channels     (bindings)     PK (account, plugin, channel_id)
```

Assignments and bindings **reference registry entries**; removing a
registry entry is refused while dependent assignments/bindings
exist (explicit removal of dependents required — documented
conflict, never cascade-silent). A plugin initializes its bindings
from the account's assignments at assign time (copy), then diverges
freely; later account-assignment changes never propagate into
bindings.

### 5.2 Channel validity and lifecycle

The registry records **identity**, not current usability. Each
registry row carries: descriptive metadata (cache-sourced), a
**validation state** — `unknown | available | inaccessible | deleted
| stale` — a last-observed/validated timestamp, and a validation
reason. Cache refreshes may update metadata and validation state but
**must not silently change user-selected assignments or bindings**;
one missing cache observation never means permanent deletion.

**Pre-execution contract:** a channel-bound operation resolves its
binding AND verifies usability from runtime state. A missing, stale,
deleted, or inaccessible channel is *not* resolved: it produces a
structured visible SKIPPED (stable reason code) or a classified
failure per the step's policy — never a blind send.

## 6. Preset model

### 6.1 Ownership schema

```
presets: preset_id (stable identity)
         scope (global|workspace|account)   owner_id (NULL only for global)
         plugin_id (required binding)
         name        — uniqueness: (scope, owner_id, plugin_id, normalized_name)
         version (immutable, monotonic)     schema_version (destination plugin)
         settings_json                       created_at + descriptive metadata
```

Scope-owner consistency enforced at write; `schema_version` is the
destination plugin's settings-schema version the preset was
validated against.

### 6.2 Immutable versions

A published version never changes. Editing creates a NEW version
under the same `preset_id`. Applying records `source_preset_id` +
`source_version`, identifying the exact version applied. Retiring or
deleting a preset definition never deletes already-applied account
configurations — provenance stays readable.

### 6.3 Lifecycle

```
create → validate → apply → customize → update (explicit, diffed) → detach
```

- **validate** — the selected immutable version against the
  destination plugin's Setting schema and `schema_version`
  (ControlAPI layer, never the repository).
- **apply** — LOCKED SEMANTICS: **writes exactly the keys the preset
  defines (add/overwrite); keys the preset does not mention stay
  untouched.** The diff preview shows old→new for the preset's keys
  only.
- **customize** — subsequent edits touch account values only.
- **update** — applying a newer version is explicit: preview diff →
  confirm → atomic write. See 6.4.
- **detach** — drops the provenance link, keeps values; the source
  preset may change or be deleted freely.

### 6.4 Atomic apply / stale-preview protection

1. validate the immutable version against the destination schema;
2. read the target account-plugin configuration **revision**;
3. compute the proposed diff (preset's keys only);
4. require confirmation for the displayed diff;
5. atomically write settings + provenance **only if the revision is
   unchanged**;
6. on conflict: return the stale-diff error; the caller re-previews.

### 6.5 Provenance cardinality (v1)

**Single active preset source per account-plugin pair.** Provenance
columns live on the pair (`source_preset_id`, `source_version`,
`applied_at`). Applying a different preset replaces the provenance.
Field-level/composed provenance is explicitly out of v1.

### 6.6 Scope lifecycles

- **Workspace deleted** → its preset templates are **archived**
  (readable, no longer selectable); applied configurations and
  provenance untouched either way.
- **Account deleted** → account-owned presets are deleted with the
  account (§13.3 workflow); export beforehand is the operator's
  explicit choice.

### 6.7 Resolution chain (simplified)

```
today: defaults ← global ← bound profile ← account overrides   (live)
v10:   defaults ← global ← account values                      (copy)
```

## 7. Ownership lattice

| Resource | Global | Workspace | Account | Account + Plugin |
|---|---|---|---|---|
| Channel defaults | define | suggestions (copy) | own assignments | own bindings |
| Data presets | own templates | own templates | own templates | plugin-specific templates |
| Plugin settings | catalog/policies only | no shared settings | — | own settings |
| Plugin runtime data | none shared | none shared | — | own data |
| Plugin credentials | none shared | none shared | — | own credentials |

Conceptual key: `workspace → account → plugin (→ guild → channel,
opt-in)`. Implementation: account identity stays globally unique
(decision 4), rows key by account as today; the workspace prefix is
membership context + the **deletion boundary**. The guild/channel
levels stay opt-in (`"0"` sentinels carry over).

## 8. Entity model

```
┌──────────────────────────────┐
│ workspaces                   │
│──────────────────────────────│
│ name (UNIQUE)  description   │
│ enabled  paused  generation  │
│ created_at  updated_at       │
└──────┬───────────────────────┘
       │ at-most-one per account; accounts.workspace = THE pointer (11)
       ▼
┌──────────────────────────────┐      ┌──────────────────────────────┐
│ accounts                     │      │ workspace_members            │
│──────────────────────────────│      │ (PROJECTION — same-tx        │
│ name (PK)   workspace  ←─────┼──────┤  update only; reconciler-    │
│ enabled …                    │      │  checked; never written      │
└──────┬───────────────────────┘      │  directly) (11)              │
       │                             │ mode, eligibility, joined_at │
       │                             └──────────────────────────────┘
       ├────────────────┬─────────────┬──────────────────┐
       ▼                ▼             ▼                  ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────────┐ ┌───────────────┐
│account_      │ │account_      │ │ account_plugins  │ │ presets (+    │
│channels      │ │channel_      │ │──────────────────│ │ versions,     │
│(registry+    │ │assignments   │ │ account, plugin  │ │ §6.1 schema)  │
│ validity §5.2│ │              │ │ source_preset_id │ └───────────────┘
└──────┬───────┘ └──────────────┘ │ source_version   │ ┌───────────────┐
       ▼                          │ applied_at  (6.5)│ │ preset_       │
┌──────────────┐                  └──────────────────┘ │ versions      │
│account_plugin│                                       │ (immutable,   │
│_channels     │                                       │  §6.2)        │
│(bindings)    │                                       └───────────────┘
└──────────────┘

┌──────────────────────────────┐   ┌──────────────────────────────┐
│ workspace_chains             │   │ chain_runs                   │
│──────────────────────────────│   │──────────────────────────────│
│ id, workspace, name (UNIQUE) │──►│ chain_id, definition_snapshot│
│ schema_ver, revision         │   │  (immutable, 14), status     │
│ enabled, definition_json     │   │ created/finished timestamps  │
└──────────────────────────────┘   └──────┬───────────────────────┘
┌──────────────────────────────┐          ▼
│ workspace_tasks              │   ┌──────────────────────────────┐
│──────────────────────────────│   │ step_executions              │
│ chain_run_id, step, status   │   │──────────────────────────────│
│ target_account (persisted 13)│   │ run, step, attempt, outcome, │
│ account, task_id, generation │   │ classified error, timestamps │
│ error, timestamps            │   └──────────────────────────────┘
└──────────────────────────────┘
┌──────────────────────────────┐   ┌──────────────────────────────┐
│ signals (outbox, §9.2)       │   │ deletion_jobs (§13.3)        │
│──────────────────────────────│   │──────────────────────────────│
│ signal_id (stable), account, │   │ account, state, attempts,    │
│ plugin, name, payload,       │   │ last_error, observable       │
│ correlation, created_at,     │   │ completion state             │
│ delivery state, ack, retries │   └──────────────────────────────┘
└──────────────────────────────┘   + workspace_cooldowns, workspace_
                                     transitions (rev 2 shapes)
```

## 9. Trigger catalog (decision 1: hybrid)

### 9.1 Mechanical triggers — task-terminal events

`TaskCompleted` / `TaskFailed` carry account + task_id today but no
`plugin`; the additive `plugin: str = ""` extension lands as W4's
first commit (scheduler holds it at emit time).

### 9.2 Semantic triggers — durable signal delivery (decision 12)

```python
async def emit_signal(self, name: str, payload: dict | None = None) -> None
```

**Contract (at-least-once, idempotent):**

- `emit_signal` persists a signal row (outbox) FIRST — signal_id,
  originating account + plugin, name, validated payload, creation
  timestamp, optional correlation id (originating task or chain
  run) — and only then acknowledges. Queue pressure may delay
  delivery; it can never discard an accepted signal.
- Names are plugin-namespaced at consumption (`<plugin>:<name>`).
- Delivery tracks attempt + acknowledgement state with retry and
  terminal dead-letter classification; invalid payloads are rejected
  at emission or recorded as classified terminal errors.
- Consumers (the chain runtime) deduplicate by `signal_id` and make
  run creation/advancement idempotent.
- Signals AUTHORIZE NOTHING (fleet's §3 rule carries over).
- Retention: delivered signals and terminal failures are cleaned on
  a schedule (retention window configured, default bounded).

## 10. Chain model

### 10.1 Plugin declarations (static)

```python
EMITS: ClassVar[tuple[str, ...]] = ("claim_done",)
FOLLOWS: ClassVar[tuple[FollowSpec, ...]] = (
    FollowSpec(signal="grinder:claim_done", handler="on_claim_done"),
    FollowSpec(task_action="message", handler="on_sender_message"),
)
```

Load-time validation: hooks must exist; unknown signal namespaces
warn, not error.

### 10.2 Step routing (decision 13)

Each step carries `routing`:

- `specific_account: <member>` — the configured member is the
  target;
- `first_eligible` — exactly one member by deterministic order:
  **membership seniority (joined_at ASC), account name ASC
  tie-break**, evaluated against the §12 gate. The selection is
  **persisted before execution**; retries and recovery reuse the
  same target, never re-select.
- No eligible target → visible SKIPPED with a stable reason code and
  the eligibility diagnostics of each considered member.
- Fan-out (`all_eligible`) is out of v1.

### 10.3 Definition versioning (decision 14)

Chain definitions carry a schema version + monotonically increasing
revision, validated on save. A run captures an immutable snapshot at
creation; edits affect future runs only. Active runs keep executing
their captured definition.

### 10.4 Execution identity + recovery

Durable identities: chain run, step execution, attempt, originating
signal/task. `step_executions` persists attempt count, timestamps,
target account, outcome, classified error.

Restart reconciliation (explicit policy, no blind replay): `RUNNING`
steps reconcile against the task system's truth — a task that
completed advances the run; a task that vanished without a terminal
event re-queues ONLY if the step declares replay-safe handling (the
execution context carries an idempotency key plugins may use);
otherwise the step fails with a classified recovery error.
Retryable vs terminal errors, max attempts, backoff, cancellation,
prerequisite-skip, and `on_skip: continue|abort` are per-chain
configuration; partial completion is a first-class terminal state
(runs record which steps completed/skipped/failed).

## 11. Execution contracts (decision 17 — clock ≠ mutex)

| Contract | v1 rule | Enforced by |
|---|---|---|
| Rate coordination | members share the `workspace:<name>` clock/stamps; last-leaver drops them | RateLimitManager (carried semantics) |
| Task concurrency | **unrelated tasks on different accounts run concurrently**; within one account the existing per-account lane (concurrency 1) serializes | TaskScheduler lanes (unchanged) |
| Chain ordering | steps within a run are sequential; **independent runs may overlap** | chain runtime |
| Mutation serialization | workspace membership/pause/generation ops under the workspace lock; per-account mutations under the account lane; preset apply is revision-checked (6.4) | service locks + revisions |

The shared clock paces; it never blocks execution itself.

## 12. Eligibility gate (fleet's check order, extended)

1. framework safety (estop / scheduler halted)
2. workspace enabled ∧ not paused (generation current)
3. account connected ∧ member eligibility permits
4. workspace cooldowns permit (most-restrictive wins)
5. task assignment valid (generation match — the fence)
6. prerequisites met (chain position ordering)
7. **NEW: plugin assigned ∧ enabled ∧ chain gate passes ∧ a channel
   binding resolves AND is usable (§5.2)**

## 13. Migration (decision 5: v10) and deletion

### 13.1 Profile materialization — exact algorithm

For each bound account-plugin pair:
1. resolve effective settings under **exact v9 rules**
   (defaults ← global ← profile ← account);
2. preserve account overrides per existing precedence;
3. extract the reserved `channels` key into `account_plugin_channels`
   bindings;
4. write account-owned settings + provenance
   (`source_preset_id`, `source_version`, `applied_at=migration`);
5. **verify post-migration effective settings equal pre-migration
   effective settings**;
6. remove the old live profile reference only after verification
   succeeds.

Unknown/obsolete/invalid values are preserved verbatim or fail the
migration explicitly — never silently discarded.

### 13.2 Guarantees + tests

Single-SQLite mutations run in one transaction (plugin-data is a
separate store — see 13.3). Migration is version-gated and
restart-safe. Test matrix: no profile; profile without account
overrides; overlapping profile/account keys; reserved `channels`;
multiple accounts sharing a profile; unknown/obsolete keys; repeated
invocation; rollback halfway. Invariant: **v10 effective values ==
v9 effective values before the profile layer is removed.**

### 13.3 Cross-store account deletion (decision 18)

One transaction cannot atomically span the two stores, so deletion
is a **durable workflow** (`deletion_jobs`):
1. mark deletion pending (durable job row); prevent new account
   work; invalidate generations;
2. remove account-owned records in the primary store (in one
   transaction);
3. invoke idempotent `PluginDataStore.drop_account(account)`;
4. retry failures with backoff; cleanup status observable;
5. mark complete only after both stores confirm; after bounded
   retries, residual data is **quarantined** (flagged, retained,
   warning surfaced) — never silently abandoned.

The workflow survives process termination (resumed at boot by the
reconciler).

## 14. API surface (decision 7: clean replace)

```
workspaces_list/get/create/update/delete
workspaces_add_account / remove_account / set_account
workspaces_chains_list / chain_get / chain_set
workspaces_tasks / workspaces_op(op)

accounts_channels_list / channel_add / channel_remove / channel_set
accounts_plugin_channels_get / set
presets_list / get / create / update / delete / apply / detach
presets_export / import
```

**Mutation contract (every write op):** canonical identifiers/scope;
validation + authorization; atomicity; idempotency; result shape;
conflicts/failures; revision preconditions. Specifically:
- channel ops: adding/removing ONE channel is distinct from
  replacing the account's full set (separate verbs);
- plugin bindings: set-one vs replace-all are separate verbs;
- preset apply/update returns applied version, diff/result, and the
  resulting configuration revision;
- membership ops return resulting membership AND generation;
- chain save returns the validated definition revision.

CLI mirrors the same verbs; `groups`/`fleet` verb groups retire.

## 15. UI navigation (future Flet surface — reference)

| Area | Manages |
|---|---|
| Global → Channels | policies + defaults |
| Workspace → Channels | organization + suggested defaults |
| Account → Channels | the account's assignments (+ validity states) |
| Account → Data Presets | account-owned presets |
| Account → Plugins | enabled plugins, independent configurations |
| Account → Plugins → Plugin | settings, bindings, presets, data |
| Extensions → Preset Catalog | discoverable templates |

Workspace views AGGREGATE; editing resolves to an explicit account
context.

## 16. Carry-over vs new surface

| Carries over (renamed) | New |
|---|---|
| transitions + validators | chain runtime + routing + snapshots |
| generation fences | signals outbox + durable delivery |
| reconciler passes (+ deletion-job resume) | channel validity lifecycle |
| policy check order | preset versions + atomic apply |
| single-lock writer discipline | membership projection invariant |
| shared-clock semantics | mutation-contract surface |
| cooldown records | deletion workflow + quarantine |

## 17. W0 exit criteria — RESOLVED (rev 3)

- [x] One authoritative membership representation — pointer (11)
- [x] Transactional membership move/add/remove — §3 note + §8; tests in §B of the review input adopted as W3 race tests
- [x] Durable signal delivery + idempotent consumption — §9.2 (12)
- [x] Deterministic per-step routing — §10.2 (13)
- [x] Chain definition snapshot/version behavior — §10.3 (14)
- [x] Shared clock separated from concurrency — §11 (17)
- [x] Preset identity, uniqueness, immutable versions — §6.1-6.2 (15)
- [x] Atomic preset apply with stale-revision protection — §6.4 (16)
- [x] Channel validity + stale-binding behavior — §5.2
- [x] v9→v10 effective-settings preservation algorithm + failure tests — §13.1-13.2
- [x] Cross-store deletion workflow + recovery — §13.3 (18)
- [x] Mutation API contracts — §14

## 18. Scope guard (rev 3 §K, retained)

Nothing in this spec authorizes: changes to the locked
account/plugin ownership model; multi-workspace membership;
automatic cross-account settings propagation; automatic preset
updates; cross-plugin compatibility guessing; or chain fan-out in
v1. Implementation starts only on explicit owner approval — which
this document now awaits as its single remaining gate.
