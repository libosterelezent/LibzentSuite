# WORKSPACE_DESIGN.md — W0 rev 3 amendment specification

Status: **Proposed amendments to W0 rev 2 — owner review required before W1.**

Source: `WORKSPACE_DESIGN.md` (W0 SPEC rev 2, 2026-11-14).

This is an additive change specification. It preserves existing W0 decisions unless explicitly amended below. It is not authorization to modify the repository or begin implementation.

---

## A. Proposed decision record additions

Add these proposed decisions to §2. They are not locked until owner approval.

| # | Decision | Proposed choice |
|---|---|---|
| 11 | Membership source of truth | `accounts.workspace` is the sole authoritative membership pointer for v1. `workspace_members` is removed or maintained only as a transactionally updated projection; it is never independently writable. |
| 12 | Signal delivery | Chain-triggering signals are durably accepted before acknowledgement and delivered at least once. Consumers must be idempotent. |
| 13 | Chain target selection | Each step has an explicit routing policy. V1 supports `specific_account` and deterministic `first_eligible`; fan-out is deferred. |
| 14 | Chain definition changes | Each run executes against an immutable validated definition snapshot/version captured at run creation. |
| 15 | Preset versions | Preset versions are immutable. Updating creates a new version; applied account configurations retain their source version and copied values. |
| 16 | Preset application concurrency | Apply/update is atomic and revision-checked. A stale preview cannot overwrite intervening account edits. |
| 17 | Shared clock versus execution | Workspace clock coordination does not itself imply global task serialization. Rate-limit coordination, task concurrency, chain ordering, and mutation serialization are separate contracts. |
| 18 | Account deletion | Cross-store cleanup is a durable, retryable workflow with observable completion. |

## B. Amend §8 — membership invariant

The current entity diagram shows both `accounts.workspace` and `workspace_members`. Define one source of truth.

**Proposed v1 rule:** `accounts.workspace` is authoritative and is the only writable membership pointer. If `workspace_members` is retained for query performance, it is a derived projection updated in the same database transaction and checked by an invariant/reconciliation mechanism. Alternatively, make `workspace_members` canonical and remove `accounts.workspace`. Do not permit both to be independently writable.

Membership add, move, and removal operations must define their transaction boundary. A workspace move must consistently update membership, generation fencing, shared-clock registration, member eligibility, and workspace-owned state.

Account-owned plugin settings, plugin data, account channel assignments, and account-plugin channel bindings remain account-owned when an account leaves a workspace. Membership changes must not delete or silently rewrite them.

Required tests:
- An account cannot belong to two workspaces.
- The canonical membership and any projection cannot disagree after commit.
- A failed membership operation leaves prior membership intact.
- Repeated add/remove requests are idempotent or return a documented conflict.
- Moving an account invalidates stale workspace work without deleting account-owned configuration.

## C. Add §5.2 — channel validity and lifecycle

The channel registry records known identity; it does not prove current usability.

Define separate identity, descriptive cache metadata, validation state, last-observed/validated timestamp, and validation reason. Suggested states are `unknown`, `available`, `inaccessible`, `deleted`, and `stale`; finalize the exact enum against the existing adapter/cache contracts.

Before a channel-bound operation executes, resolve the binding and verify usability using available runtime information. A missing, stale, deleted, or inaccessible channel must not be treated as successfully resolved. Produce a structured visible skip or classified failure according to the operation policy; never attempt a blind send.

Define the registry uniqueness key. Proposed key: `(account, guild_id, channel_id)`, unless the platform identity model proves `channel_id` globally sufficient and the guild relationship is separately constrained. Assignments and plugin bindings must reference registry entries. Removing a registry entry must have explicit behavior for dependent rows.

Cache refreshes may update metadata and validation state, but must not silently change user-selected assignments or plugin bindings. One missing cache observation must not automatically mean permanent deletion.

## D. Amend §6 — preset ownership, versions, and application

### D.1 Ownership schema

Each preset must identify its owner unambiguously. At minimum define:
- `preset_id` — stable identity;
- `scope` — global, workspace, or account;
- `owner_id` — null only for global scope;
- `plugin_id` — required plugin binding;
- `name`;
- `version` — immutable version number;
- `schema_version` — destination plugin settings schema version;
- `settings_json`;
- creation and descriptive metadata.

Enforce scope-owner consistency. Preset name uniqueness should be scoped to `(scope, owner_id, plugin_id, normalized_name)`, not globally across unrelated owners.

### D.2 Immutable versions

A published preset version is immutable. Editing creates a new version under the same preset identity or a new immutable version record associated with it. `source_preset_id` plus `source_version` must identify the exact applied version.

Retiring/deleting a preset definition must not delete account-plugin settings already copied from it. Existing provenance remains readable.

### D.3 Atomic apply and stale-preview protection

Preset application must:
1. validate the selected immutable version against the destination plugin and schema;
2. read the target account-plugin configuration revision;
3. compute the proposed diff;
4. require confirmation for the displayed diff;
5. atomically write resulting settings and provenance only if the target revision is unchanged;
6. return a conflict and require a refreshed diff if the target changed after preview.

Specify whether application replaces the full configuration or merges selected keys. Do not leave this implicit.

### D.4 Provenance constraint

If v1 supports only one active preset source per account-plugin pair, state that constraint explicitly. If partial application or multiple composed presets are supported, pair-level provenance is insufficient; use application records or field-level provenance.

### D.5 Workspace and account preset lifecycle

Define what happens to workspace presets when a workspace is deleted: archive, transfer, or delete the template definition. In every case, deleting a source preset must not alter already-applied account configurations.

Define export/retention behavior for account-owned presets before account removal.

## E. Amend §9.2 — durable signal delivery

“Fire-and-forget” and “never dropped under queue pressure” do not fully specify delivery.

Define:
- stable `signal_id`;
- originating account and plugin;
- signal name and validated payload;
- creation timestamp;
- optional correlation ID / originating task or chain run;
- durable acceptance point;
- delivery attempt and acknowledgement state;
- retry and terminal/dead-letter behavior;
- deduplication/idempotency key.

**Proposed contract:** persist the signal or an outbox record before acknowledging successful emission. Deliver at least once. Consumers deduplicate by stable signal identity and make chain creation/advancement idempotent.

Queue pressure may delay delivery but cannot discard an accepted signal. Invalid payloads must be rejected at emission or recorded as a classified terminal error. Define retention and cleanup for delivered signals and terminal failures.

## F. Amend §10 — routing and chain execution

### F.1 Explicit target routing

Add a routing field to each chain step. Proposed v1 values:
- `specific_account`: a configured workspace member is the target;
- `first_eligible`: choose exactly one eligible member using a deterministic ordering.

Define the ordering and tie-breaker for `first_eligible`. Persist the selected account before execution. Retries and recovery must reuse that target rather than selecting a different account.

Defer `all_eligible`/fan-out until completion, partial-success, retry, and cancellation semantics are specified. If no target is eligible, record a visible `SKIPPED` result with a stable reason code and diagnostic eligibility information.

### F.2 Chain definition versioning

Give each chain definition a schema version and monotonically increasing revision. Validate on save. When a run starts, persist an immutable validated snapshot or immutable version reference. Editing a chain affects future runs only unless a separate active-run update operation is designed.

### F.3 Execution identity and recovery

Define durable identities for chain run, step execution, attempt, and originating signal/task. Persist attempt count, timestamps, target account, outcome, and classified error information.

On restart, reconcile `RUNNING` steps according to an explicit recovery policy. Do not blindly re-execute potentially non-idempotent operations. Handlers should receive an idempotency/execution context or document why replay is safe.

Specify retryable versus terminal errors, maximum attempts, backoff, cancellation, prerequisite failure/skip behavior, `on_skip` semantics, and partial completion. A run continues using its captured definition after a chain is edited.

## G. Amend §11 — separate pacing from concurrency

Replace ambiguous “serializes fully” wording with four contracts:

1. **Rate-limit coordination:** workspace members share the configured workspace clock/stamps.
2. **Task concurrency:** define whether unrelated tasks on different members may run concurrently.
3. **Chain ordering:** define sequential ordering within a run and whether independent runs may overlap.
4. **Mutation serialization:** define whether operations targeting the same account, channel, message, or remote resource require a narrower lock.

A shared clock is not, by itself, an execution mutex. If v1 intentionally preserves full serialization, specify exactly which operations are serialized and which lock/queue enforces it.

## H. Amend §13 — migration and deletion guarantees

### H.1 Profile materialization algorithm

Define the migration as:
1. Read each bound account-plugin pair.
2. Resolve effective settings under exact v9 rules.
3. Preserve account overrides according to existing precedence.
4. Extract reserved `channels` into account-plugin channel bindings.
5. Write account-owned settings and provenance.
6. Verify post-migration effective settings against pre-migration effective settings.
7. Remove the old live profile reference only after successful materialization.

Unknown, obsolete, or invalid values must be preserved or cause explicit migration failure; never silently discard them.

### H.2 Migration guarantees and tests

Where affected tables share one SQLite database, use a transaction. Migration must be version-gated and restart-safe.

Test: no profile; profile without account overrides; overlapping profile/account keys; reserved `channels`; multiple accounts sharing a profile; unknown/obsolete keys; repeated invocation; rollback/failure halfway through.

The invariant is that v10 produces the same effective values as v9 before the old profile layer is removed.

### H.3 Cross-store account deletion

Do not imply one SQLite transaction can atomically delete the account row and plugin-data documents if `PluginDataStore` is a separate store.

Define a durable workflow:
1. mark deletion pending or create a durable deletion job;
2. prevent new account work and invalidate relevant generations;
3. remove account-owned records in the primary store;
4. invoke idempotent `PluginDataStore.drop_account(account)`;
5. retry failures and expose cleanup status;
6. mark deletion complete only after required stores confirm cleanup.

The workflow must survive process termination. Define quarantine/retention behavior for data that cannot be deleted after bounded retries.

## I. Amend §14 — mutation API contracts

For every mutating ControlAPI operation, document canonical identifiers/scope, validation and authorization, atomicity, idempotency, result shape, conflicts/failures, and revision preconditions.

In particular:
- distinguish adding/removing one channel from replacing the full account assignment set;
- distinguish setting one plugin binding from replacing all bindings;
- make preset apply/update return the applied version, diff/result, and resulting configuration revision;
- make workspace membership operations return resulting membership and generation;
- make chain save return the validated definition revision.

Avoid ambiguous `set` operations with undocumented replacement semantics.

## J. Required W0 exit criteria before W1

W1 should not begin until the owner has accepted the proposed decisions and these contracts are unambiguous:

- [ ] One authoritative workspace membership representation.
- [ ] Transactional membership move/add/remove behavior.
- [ ] Durable signal delivery and idempotent consumption.
- [ ] Deterministic per-step account routing.
- [ ] Chain definition snapshot/version behavior.
- [ ] Shared-clock semantics separated from task concurrency.
- [ ] Preset owner identity, uniqueness, and immutable version model.
- [ ] Atomic preset application with stale-revision protection.
- [ ] Channel validity and stale-binding behavior.
- [ ] v9→v10 effective-settings preservation algorithm and failure tests.
- [ ] Cross-store account deletion workflow and recovery.
- [ ] Mutation API contracts for all new write operations.

## K. Scope guard

This amendment does not authorize direct repository changes, schema/API implementation, changes to the locked account/plugin ownership model, multi-workspace membership, automatic cross-account settings propagation, automatic preset updates, cross-plugin compatibility guessing, or chain fan-out in v1.

Implementation remains subject to explicit owner approval of the revised W0 specification.
