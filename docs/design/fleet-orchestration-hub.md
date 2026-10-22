# Fleet Orchestration Hub — Design Spec (PHASE 9)

Status: DECISIONS LOCKED by the operator (2026-10-22). Sources: GPT consult
(`state/ui-rebuild-2026-10-22/consult-B2-fleet-hub-gpt.md`, repo-connected),
lead-engineer counter-positions, operator answers. Companion: PHASE 10
(plugin metrics) in `TODO + Ideas.md`, and the B3 account-data discussion
(this spec's §7 is its seed).

## 0. Locked decisions

| # | Decision | Locked |
|---|---|---|
| D1 | Membership scope | **Scheduling ownership** + explicit `INDEPENDENT \| GROUP_MANAGED` execution-mode flag. Group-managed = the hub has full control of what the account does and when; the Independent flow is suspended; leaving restores it. The limiter and account config stay independent of membership. |
| D2 | Authoritative runtime state | **Database-authoritative** (transitions commit to DB; in-memory state is a projection) + a modest transition-history table. No event sourcing. |
| D3 | Task assignment | **Operator assigns + system-assisted reassignment**, with a **strategy slot**: `manual` (default) and `relay` (limit observation → suspend owner's task → scoped cooldown → hand task to next eligible member). New strategies plug in later. |
| D4 | Transition modes | **Explicit modes, DRAIN default** (stop accepting new work, let in-flight finish; in-flight paced sends are <2s today). SUSPEND arrives when long-lived loops exist. |
| D5 | Task scope | **Channel-scoped from day one.** A task declares the channel resources it works on; per-channel plugin data (B3) is the task's config source. |
| D6 | Hub surface | **Dedicated Fleet Hub** (bespoke SPA route) — `#/groups` list + `#/groups/<name>` hub. The Accounts page keeps per-account management. |

## 1. Architecture — who owns what

    SyncGroupManager   membership + persistence (UNCHANGED JOB)
    FleetOrchestrator  NEW: fleet state, eligibility, coordination, reconciler
    GroupPolicyEngine  policy decisions (may this account run this task now?)
    Task scheduler     execution lifecycle (EXISTS — reused, not replaced)
    RateLimitManager   pacing safety floor (EXISTS — never learns fleet state)
    Parser engine      structured observations (EXISTS — gains limit rules)
    EventBus           transport (EXISTS — three new event families)

Central rule: **membership determines the fleet; policy determines whether a
task may run; the scheduler manages execution; the limiter determines when an
approved action may proceed. None absorbs the others.**

Implementation shape: start as ONE module (`core/fleet/`) with internal
components (state machine, policy checks, transitions); extract classes when
testing needs justify it. Do not create a manager per noun.

## 2. Member model — two axes in v1 (lifecycle stays in account status)

Fleet eligibility (per member, per group):
`ELIGIBLE → WAITING → (PAUSED | COOLING) → back to ELIGIBLE/WAITING`,
plus `RETIRED` on leave. Account connectivity is DERIVED from account status,
never duplicated.

"Spent" is not an eligibility state: a detected limit registers a scoped
COOLDOWN (source=`observed`, expiry=the game's own window) on
(account, task, channel scope) and the strategy reassigns the task.

Task states: `QUEUED → ASSIGNED → RUNNING → COMPLETED | SUSPENDED | FAILED |
CANCELLED`. SUSPENDED returns to QUEUED when prerequisites clear; FAILED
resumes only if the retry policy explicitly allows.

## 3. Signals — three families over the existing EventBus

- **Observations** (authorize nothing): parser classified a limit marker;
  account disconnected; limiter reported a server cooldown.
- **Commands** (validated by FleetService, never trusted from the wire):
  pause/resume group, pause/resume member, assign/withdraw task, suspend,
  handoff, apply/clear manual cooldown.
- **State changes** (authoritative, persisted): GroupStateChanged,
  MemberEligibilityChanged, TaskAssigned/Started/Finished/Withdrawn,
  CooldownStarted/Ended, AssignmentHandoff, RecoveryRequired.

Every authoritative event carries: `event_id, group_id, generation,
correlation_id, occurred_at, reason` (+ task/assignment/account where apt).
Delivery uses the ORDINARY bus lane; critical transitions are persisted
first (write-ahead), delivery is an optimization. Dedup key for observations:
`(source_account, message_id, observation_type)`.

## 4. Policy vs pacing — the load-bearing interface

    PolicyDecision evaluate(account, task, group) ->
        { allowed: bool, reason: str, retry_at: ts|None }

Check order: framework safety → group enabled/not paused → account connected
+ eligible + not paused → cooldowns permit (most-restrictive wins) → task
assigned + valid (generation match) → prerequisites satisfied.

Then: scheduler dispatch → `limiter.acquire(...)` (EXISTS, untouched) →
FINAL policy revalidation → execute. A task approved at enqueue can still be
rejected at dispatch (group paused mid-queue). The limiter may REPORT that a
cooldown delayed an action; the orchestrator consumes that as an observation.

Never add to the limiter: choosing the active member, task ownership, group
pause state, game-message interpretation, next-account selection, history.

## 5. Task definitions — channel-scoped from day one (D5, seeds B3)

    TaskDefinition:            # declared by a plugin (opt-in capability)
        id                     # e.g. "roll"
        plugin                 # owning plugin
        channel_scope          # which channel(s) the task operates on
        start/stop conditions  # schedule, prerequisites, manual stop
        resume_policy, max_attempts
        data_key               # -> B3 per-channel plugin data as task config

    TaskAssignment:
        id, group_id, task_id, account_id, status, priority,
        created_at, started_at, stopped_at, resume_policy, version, generation

Rules: task identity + audit history survive handoff; a task assigned under
group generation N is rejected at dispatch if the live generation differs;
handoff = explicit transfer op (validate destination → revoke source → create
destination assignment → persist → notify); a failed destination check never
destroys the source assignment.

Plugins opt in via a `FleetTaskHandler` capability on PluginBase (default
OFF — `ping` and simple plugins are untouched): prepare/execute/suspend/
recover. Plugins never own membership, fleet cooldowns, or cross-account
state, and never run private timer loops for scheduled work — the central
scheduler owns the lifecycle.

## 6. Cooldowns — first-class scoped records

    Cooldown: id, scope_type[group|account|task], scope_id, reason,
              started_at, expires_at, source[manual|observed|server]

Most-restrictive-wins through ONE policy interface. Clearing a manual
cooldown never clears an `observed`/`server` cooldown. Dashboard shows the
effective cooldown AND its source.

## 7. Account data (B3 bridge — to be expanded with the operator's input)

The channel-scoped task (D5) is the anchor: per-channel, per-plugin settings
and cooldown data (the failed ProfileManager goal) becomes "the data a task
reads", keyed by (account, plugin, channel-scope). Metrics then roll up
naturally: LOCAL = per task/channel scope; GLOBAL = per account/plugin.
Full data model = the B3 discussion, next.

## 8. Join / leave — the ownership transition (D1 + D3/D4)

JOIN (FleetService, transactional-by-compensation):
  validate → set mode GROUP_MANAGED (block new independent dispatch) →
  drain in-flight independent work (D4) → persist membership → sync limiter
  mapping → initialize member runtime state → re-evaluate tasks → publish.
LEAVE: reverse (block group dispatch → revoke/suspend assignments → persist
removal → clear runtime mapping → restore INDEPENDENT → reconcile → publish).
Stale work is rejected by generation check, never by goodwill.

## 9. Reconciler — correctness never rides the event bus

Runs at boot, on WS reconnect, and every 30s: compares persisted membership,
execution modes, assignments (leases: owner, generation, expiry, heartbeat),
cooldown records, limiter mapping, account runtime status. Repairs what is
safe; surfaces ambiguity for review. Transitions are idempotent
(operation ids + version checks); lease reclaim fences the old owner.

## 10. Fleet Hub (D6) — bespoke SPA view

Sections: fleet overview (status, members, task counts, active cooldowns,
last transition — "what is this group doing and why"), member queue (state,
eligibility reason, assigned work, controls), task queue (filters by state,
next-eligible time, failure reasons), handoff/transition history (append-only,
actor-tagged: automated vs operator vs parser), overrides (pause/resume group,
pause/resume member, timed cooldown, assign, withdraw, suspend, handoff —
destructive ops confirmed, multi-account ops preview affected members).
Live via the existing WS/store; mutations through FleetService on the server.

## 11. Rollout (acceptance criteria per phase)

A. Domain models + transition validation — transitions testable with no
   Discord connection.
B. Orchestration service — every operation flows through one service from
   dashboard, CLI, or tests.
C. Scheduler + limiter integration — a paused group cannot start new work;
   policy changes never bypass pacing.
D. Parser observations — parser output alone cannot bypass state/policy.
E. Ownership transitions — no account receives conflicting work from both
   paths; leave restores Independent cleanly.
F. Fleet Hub — the operator can see WHY a task is running/waiting/suspended
   without reading logs.
G. Recovery + regression — restart, disconnect, duplicate/missed events,
   concurrent admin ops, stale dispatches; state converges without duplicate
   execution.

## 12. Deferred by design

Event sourcing; RETIRED/BLOCKED eligibility refinements; SUSPEND transition
mode (until long-lived loops exist); multi-group membership ( stays
single-group); capability-based auto-scheduling (the strategy slot admits it
later without schema change).
