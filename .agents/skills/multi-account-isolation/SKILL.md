---
description: Design, implement, review, and test strict isolation
  between multiple external-platform accounts managed by one
  application. Use when changing account ownership, plugin state,
  guild/channel data, workers, events, caches, persistence, credentials,
  concurrency, recovery, or any code that can read or mutate
  account-scoped state.
name: multi-account-isolation
---

# Multi-Account Isolation

## Mission

Ensure that every managed account operates inside an explicit,
verifiable isolation boundary. An account must not read, mutate,
receive, or influence another account's state unless a specific
cross-account operation is deliberately authorized, narrowly scoped, and
auditable.

This skill is for applications that manage multiple accounts belonging
to external platforms within one application/runtime. It is **not** a
generic SaaS tenant-isolation recipe. Do not assume separate databases,
PostgreSQL Row-Level Security (RLS), schema-per-tenant, containers, or
separate processes are available or appropriate.

For LibzentSuite, treat the known architecture as context to verify
against the current code, not as permission to assume implementation
details:

-   Python application with asynchronous execution and SQLite-backed
    persistence.
-   One core API serving more than one interface.
-   Account-bound plugin configuration and plugin data.
-   A hierarchical ownership model involving account, plugin, guild, and
    channel scopes.
-   Some scope dimensions may use sentinel values such as `"0"` to
    represent a global/default scope. Verify their exact meaning and
    constraints in code before relying on them.

Do not conflate LibzentSuite with other projects or copy their
architecture into this application.

## When to use this skill

Use this skill whenever a change touches, or could indirectly affect:

-   Account creation, selection, login, credentials, enable/disable,
    deletion, or switching.
-   Plugin installation, configuration, activation, execution, or
    plugin-owned data.
-   Guild, channel, server, workspace, or other resource ownership.
-   API routes, CLI commands, UI actions, callbacks, background jobs, or
    event handlers.
-   SQLite schemas, queries, transactions, migrations, or repository
    methods.
-   In-memory state, caches, registries, queues, locks, rate limits, or
    shared clients.
-   Task scheduling, retries, cancellation, reconnects, shutdown, and
    restart recovery.
-   Logging, diagnostics, exports, backups, and error reporting.
-   Tests, fixtures, mocks, or utilities that create or select account
    context.

If uncertain whether a change is account-sensitive, treat it as
account-sensitive until the data flow proves otherwise.

## Non-negotiable invariants

1.  **Explicit identity:** Every account-scoped operation receives an
    explicit, validated account identity. Never infer it from mutable
    global state, the currently selected UI account, a previous request,
    or whichever worker ran last.
2.  **Ownership before access:** Resolve and validate ownership before
    reading or mutating account-scoped resources. A caller-supplied
    account ID is a claim, not proof of authorization.
3.  **Scope preservation:** Every layer preserves the complete ownership
    scope required by the resource. Do not drop account or plugin scope
    while traversing service, repository, event, or task boundaries.
4.  **Fail closed:** Missing, malformed, ambiguous, stale, or
    conflicting account context must fail safely. Never silently fall
    back to another account or to a global/default scope.
5.  **No accidental global state:** Mutable state is account-scoped
    unless it is intentionally shared, documented as shared, safe to
    share, and protected against cross-account leakage.
6.  **No implicit cross-account access:** Cross-account operations
    require a dedicated API, explicit authorization, a declared purpose,
    and tests. Do not implement them by bypassing ordinary ownership
    checks.
7.  **Isolation survives concurrency:** Correctness must hold with
    simultaneous requests, workers, events, retries, reconnects, and
    account switching.
8.  **Isolation survives lifecycle changes:** Restart, recovery,
    migration, deletion, disablement, and partial failure must not
    reassign one account's state to another.
9.  **Secrets stay bound:** Credentials and sensitive account data must
    not leak through logs, exceptions, caches, debug endpoints, exports,
    or another account's UI/API response.
10. **Evidence, not assumptions:** Do not claim isolation because IDs
    are different, a class is named `Account*`, or tests use separate
    fixtures. Trace the actual read/write path and prove the boundary.

## Required ownership model

Before changing code, discover and document the actual ownership graph
from the implementation and schema.

For LibzentSuite, examine whether the relevant resource is owned by:

-   Account
-   Account + plugin
-   Account + plugin + guild
-   Account + plugin + guild + channel
-   A deliberately global scope
-   A derived or externally owned resource

Do not assume every resource uses every dimension. Define the owner of
each resource explicitly.

### Scope and sentinel rules

If the application uses sentinel values such as `"0"` for global/default
guild or channel scopes:

-   Find the canonical definition and all read/write paths.
-   Distinguish a true global scope from a missing, unknown, or
    uninitialized value.
-   Validate allowed combinations (for example, whether a global channel
    scope is valid under a specific plugin scope).
-   Prevent a sentinel from being used to escape an account boundary.
-   Do not use truthiness checks to distinguish IDs; `"0"` is truthy in
    Python, while `0`, `None`, `""`, and `"0"` may have different
    meanings.
-   Normalize identifiers at a single boundary and reject ambiguous
    representations.
-   Add uniqueness constraints and tests for valid and invalid scope
    combinations.

A global/default scope is not automatically shared across accounts.
Determine whether it means global within one account, global within one
plugin, or truly application-wide. Encode that distinction in the schema
and APIs.

## Engineering workflow

Follow this sequence for any isolation-sensitive task.

### 1. Map the entry points

Identify every way the affected operation can be initiated:

-   HTTP/API route
-   CLI command
-   UI action or callback
-   Plugin entry point
-   Event listener/subscriber
-   Background worker or scheduler
-   Startup/recovery path
-   Test helper or administrative operation

Record where account identity originates, who validates it, and how it
reaches the final data access or side effect.

### 2. Trace the complete data flow

Trace the operation end to end:

`entry point → identity/context resolution → authorization → service → repository/database → cache/event/worker → external side effect`

For each hop, answer:

-   What is the account ID?
-   Who supplied it?
-   Has ownership been verified?
-   Is the plugin/resource also validated against that account?
-   Can the value be overwritten, omitted, or taken from ambient state?
-   Does the next layer preserve the full scope?
-   What happens if the account is deleted, disabled, switched, or
    becomes unavailable mid-operation?

Inspect both reads and writes. A correctly scoped write does not
compensate for an unscoped read.

### 3. Define the boundary and invariants

Write down the resource owner, allowed callers, allowed scope
combinations, and expected behavior for missing or conflicting context.
Identify intentionally shared resources separately.

### 4. Inspect all shared-state surfaces

Use the sections below as a checklist. Do not stop at the database
layer.

### 5. Implement the narrowest safe change

Prefer explicit context objects and APIs over ambient state. Keep
ownership checks close to the service/repository boundary so they cannot
be bypassed by a new caller. Avoid broad refactors unless needed to
establish the boundary.

### 6. Test isolation negatively and concurrently

Test both allowed access and denied cross-account access. Include
simultaneous operations and lifecycle transitions where relevant.

### 7. Review the final diff

Search for alternate access paths, omitted scope parameters, global
mutable state, stale caches, broad exception handling, and tests that
accidentally share the same account fixture.

### 8. Report evidence

State what was traced, what was tested, what remains unverified, and any
accepted limitations. Never describe an untested assumption as a
guarantee.

## Isolation requirements by layer

### A. Identity and authorization

-   Use stable internal account identifiers, not usernames, display
    names, tokens, or platform-specific labels as ownership keys.
-   Resolve the account from a trusted authenticated/session context or
    an explicitly authorized internal caller.
-   Treat IDs received from request bodies, query strings, headers, CLI
    arguments, plugin payloads, and event data as untrusted until
    validated.
-   Verify that the requested account exists and that the caller is
    authorized to act for it.
-   Verify resource ownership separately: a valid account ID does not
    prove that a plugin, guild, channel, task, or record belongs to it.
-   Do not rely on a UI's selected account as a security boundary.
-   Avoid mutable module-level `current_account`, `active_account`, or
    similar state.
-   If context propagation uses `contextvars`, ensure tasks are created
    with the intended context and do not assume context propagation
    replaces authorization.
-   For privileged cross-account administration, use a separate explicit
    interface with least privilege and audit logging.

### B. Database and persistence

-   Include the full ownership key in reads, updates, and deletes.
    Example: fetch a record by both its record ID and its owning
    account/scope, not by record ID alone.
-   Do not fetch globally and filter in Python when the database can
    enforce the ownership predicate.
-   Use composite unique constraints and foreign keys where they
    represent real ownership relationships.
-   Ensure child rows cannot refer to a parent owned by a different
    account. Where ordinary foreign keys cannot express this, use
    composite keys or transactional validation.
-   Scope `UPDATE` and `DELETE` statements in the SQL predicate; do not
    select by global ID and mutate without rechecking ownership.
-   Use transactions for multi-step ownership validation and mutation.
    Consider concurrent changes between validation and use.
-   Treat SQLite connection, transaction, and async-wrapper behavior as
    part of the design. Do not assume a transaction is isolated merely
    because it is inside an `async` function.
-   Keep migrations backward-safe and validate existing rows for
    orphaned or contradictory ownership before enforcing new
    constraints.
-   Do not introduce PostgreSQL-only RLS as a supposed fix for a SQLite
    application. Use database constraints plus scoped repository/service
    APIs and tests appropriate to the actual storage engine.
-   Ensure backup, restore, import, export, and cleanup operations
    preserve ownership relationships.

### C. Plugin boundary

-   Treat plugin configuration and plugin data as account-bound unless
    explicitly declared otherwise.
-   Resolve plugin identity within the account's installed/enabled
    plugin set; do not trust a plugin ID supplied by a caller by itself.
-   A plugin must not obtain another account's configuration,
    credentials, data, or runtime objects through a shared registry.
-   Namespace plugin storage by the complete owner key, not only by
    plugin name.
-   Validate plugin-provided account, guild, and channel identifiers
    before persistence or execution.
-   Keep plugin defaults distinct from account overrides. Define
    precedence explicitly and never let one account's override mutate
    another account's defaults.
-   For plugin-to-core APIs, pass a constrained account/plugin context
    rather than unrestricted database or global application objects
    where feasible.
-   Ensure plugin disable/uninstall and account deletion clean up only
    the resources they own.

### D. Guild and channel scope

-   Validate that a guild/channel belongs to, or is accessible through,
    the intended account and platform connection.
-   Do not assume a channel ID is globally sufficient to establish
    ownership.
-   Preserve account + plugin + guild + channel scope through presets,
    data lookups, commands, and plugin callbacks.
-   Validate parent-child relationships; reject a channel paired with a
    guild or account it does not belong to.
-   Test global/default scope behavior independently from
    account-specific guild/channel behavior.
-   Avoid fallback queries that return another account's matching
    guild/channel record when the requested scope is absent.

### E. Runtime, workers, and tasks

-   Give each task an immutable execution context containing the account
    identity and any required plugin/resource scope.
-   Bind the task to the intended account-specific
    client/session/credential provider.
-   Do not let a worker read a mutable global "current account" at
    execution time.
-   Ensure queues carry ownership metadata and validate it again when
    dequeued; queued work may outlive account changes.
-   Keep sequential ordering or locks scoped to the correct
    account/resource. A global lock may prevent races but can
    unnecessarily couple accounts; an account-local lock must not
    accidentally protect shared state that needs a broader lock.
-   Make cancellation, retry, timeout, and exception paths preserve the
    original task owner.
-   Ensure one account's failure, cooldown, pause, disable, or reconnect
    does not stop, reset, or reschedule another account's work unless
    the behavior is explicitly global.
-   On account disable/delete, define what happens to queued, running,
    and retrying tasks. Do not allow orphaned work to execute using
    stale credentials.
-   At startup, recover interrupted work using persisted ownership
    metadata; reject or quarantine records whose owner cannot be
    resolved.

### F. Events and subscriptions

-   Include account identity in internal event envelopes whenever the
    event is account-scoped.
-   Validate the source connection/client and event resource ownership
    before dispatch.
-   Route events to subscribers using explicit account/plugin/resource
    subscriptions, not only event type or platform ID.
-   Prevent a listener registered for one account from receiving another
    account's private events or mutable message/client objects.
-   Define whether event payloads are immutable snapshots or shared
    mutable objects. Prefer immutable/defensively copied payloads across
    trust boundaries.
-   Handle duplicate, delayed, out-of-order, raw, and cached events
    without losing ownership metadata.
-   Ensure unsubscribe, plugin unload, account disable, and shutdown
    remove only the relevant subscriptions.
-   Test forged IDs and events arriving from the wrong client/account.

### G. Cache and registries

-   Include all ownership dimensions in cache keys. A key based only on
    plugin name, guild ID, channel ID, or resource ID is suspect unless
    global uniqueness and authorization are proven.
-   Namespace caches by account and plugin where appropriate.
-   Scope invalidation to the owner; one account's update must not evict
    or overwrite another account's value incorrectly.
-   Do not cache authorization decisions beyond the lifetime and
    validity of the identity/resource relationship unless invalidation
    is reliable.
-   Avoid storing account-specific objects in process-wide singleton
    registries without explicit ownership.
-   Test cache hits after switching accounts and after updating
    identical logical keys under two accounts.

### H. Concurrency and shared mutable state

-   Identify shared dictionaries, lists, sets, singleton services,
    registries, clients, connection pools, and mutable configuration
    objects.
-   Decide whether each is global, account-scoped, plugin-scoped, or
    operation-scoped. Document intentional sharing.
-   Do not return mutable internal objects to callers that belong to
    another scope.
-   Use locks around the actual shared invariant, not merely around one
    code path.
-   Avoid check-then-act ownership logic without a transaction/lock or
    other concurrency-safe guarantee.
-   Test two accounts performing the same operation simultaneously with
    colliding logical identifiers.
-   Test account switching while a request/task is in progress;
    in-flight work must retain its original context.

### I. Credentials and sensitive state

-   Never log raw tokens, passwords, cookies, authorization headers,
    encryption keys, or credential-bearing URLs.
-   Store credentials using an appropriate secret-storage/encryption
    design; keep encryption keys outside the database and source
    repository.
-   Bind credential retrieval to the immutable account ID and intended
    platform connection.
-   Do not pass credentials through plugin settings, event payloads,
    generic task metadata, or user-visible errors.
-   Ensure decryption and credential use happen only in the narrowest
    required component and for the shortest practical duration.
-   Protect backups, crash dumps, debug logs, and diagnostic exports.
-   Do not claim encryption at rest is sufficient for isolation:
    authorization and account ownership checks are still required.

### J. API, CLI, and UI boundaries

-   Apply the same ownership rules to every interface. The GUI is not
    more trusted than the API or CLI.
-   Resolve identity once at the boundary, validate it, and pass
    explicit context into application services.
-   Never let a route or command choose an arbitrary account merely by
    accepting an account ID.
-   Avoid returning records from another account even in error messages,
    counts, autocomplete, search, or pagination metadata.
-   Scope list, search, count, aggregate, and pagination queries; leaks
    often occur in these paths.
-   Ensure object IDs cannot be used for insecure direct object
    reference (IDOR) access across accounts.
-   Keep administrative endpoints separate and explicitly authorized.

### K. Lifecycle, recovery, and migrations

-   Define ownership behavior for account creation, rename, reconnect,
    disable, deletion, plugin uninstall, and reinstallation.
-   Treat account rename/display-name changes as non-identity
    operations.
-   On deletion, clean up only owned records and preserve
    audit/retention requirements.
-   On restore/import, validate that every record's owner and parent
    relationships are consistent before making it active.
-   Recovery must never guess an owner based on username, ordering,
    current selection, or the only account currently online.
-   Quarantine ambiguous or orphaned records for explicit repair rather
    than attaching them to a convenient account.
-   Test restart recovery with at least two accounts and overlapping
    task/resource identifiers.

### L. Logging and observability

-   Include stable, non-secret account identifiers in internal
    diagnostics when needed to trace ownership.
-   Do not include credentials or sensitive payloads.
-   Ensure logs, metrics, traces, and error reports do not merge
    account-private content into another account's view.
-   Scope user-visible diagnostics and plugin logs by authorization.
-   Avoid high-cardinality or sensitive labels in metrics; use safe
    identifiers or controlled aggregation.
-   Make cross-account administrative actions auditable without exposing
    unrelated account data.

## Required test strategy

A test suite must prove both isolation and legitimate access. Use at
least two distinct accounts (`A` and `B`) and deliberately create
colliding logical identifiers where possible.

### Minimum negative tests

-   Account A cannot fetch, update, or delete Account B's record by
    guessing its ID.
-   Account A cannot read or alter Account B's plugin settings or plugin
    data.
-   A plugin running for A cannot access B's client, credentials,
    guild/channel data, or runtime state.
-   A's event subscribers do not receive B's account-scoped events.
-   A's cache lookup cannot return B's cached value.
-   A's task cannot execute with B's credentials after account
    switching, reconnect, retry, or restart.
-   A cannot pair its account/plugin scope with B's guild/channel
    record.
-   Missing, malformed, stale, and conflicting account context is
    rejected.
-   A global/default sentinel cannot be used to bypass account
    ownership.
-   List, search, count, aggregate, autocomplete, and pagination
    endpoints do not leak B's records or metadata to A.
-   Account disable/delete does not leave runnable work that can use
    stale credentials.
-   Orphaned/ambiguous recovery records are rejected or quarantined, not
    reassigned.

### Minimum positive tests

-   A can access its own valid records and plugin configuration.
-   B can access its own valid records.
-   Explicitly authorized global/default resources behave exactly as
    documented.
-   Explicit administrative cross-account operations work only through
    their privileged path and are audited.
-   Independent accounts can perform valid operations concurrently
    without interference.

### Concurrency and lifecycle tests

Where relevant, test:

-   Simultaneous writes by A and B using identical logical keys.
-   Account switching while operations are in flight.
-   Event arrival during reconnect or shutdown.
-   Retry after account disablement or credential rotation.
-   Worker crash and restart recovery with multiple accounts.
-   Plugin unload/reload while tasks or events are active.
-   Migration of existing data containing missing, orphaned, or
    conflicting ownership.

Prefer deterministic barriers/events over timing-based sleeps. Assert
persisted state, emitted events, cache contents, task context, and
external calls---not merely that no exception was raised.

## Review red flags

Investigate these patterns rather than automatically declaring them
vulnerabilities:

-   `current_account`, `active_account`, or account IDs stored in
    mutable module globals.
-   Repository methods such as `get_by_id(id)` for account-owned records
    with no owner predicate.
-   SQL queries missing account/plugin scope.
-   Plugin data keyed only by plugin name.
-   Cache keys missing account identity.
-   Worker callbacks that look up the currently selected account at
    execution time.
-   Queues or events that carry only a resource ID and no source
    account/client identity.
-   A generic service exposing unrestricted database access to plugins.
-   Fallbacks that silently select the first, default, or currently
    connected account.
-   Broad exception handlers that convert authorization failures into
    empty results or successful no-ops.
-   `UPDATE`/`DELETE` statements scoped only by row ID.
-   Ownership validation performed before a long asynchronous gap and
    not revalidated where necessary.
-   Test fixtures that reuse one account, one plugin instance, or one
    database row across supposedly isolated cases.
-   Sentinel values treated inconsistently across layers.
-   Restore/recovery code that infers ownership from usernames or
    current configuration.

A red flag is a lead to trace, not a finding by itself. Report a
vulnerability only when the reachable code path and impact are
demonstrated.

## Security review output format

For an isolation-focused review, produce:

1.  **Scope and assumptions** --- components, interfaces, storage,
    runtime, and trust boundaries examined.
2.  **Ownership map** --- resource → owner dimensions → authoritative
    validation point.
3.  **Entry-point/data-flow map** --- how account context travels from
    each entry point to reads, writes, and side effects.
4.  **Findings** --- only concrete, reachable issues, each with:
    -   Severity and confidence.
    -   Affected file/function and relevant code path.
    -   Required attacker/caller capability.
    -   Step-by-step failure mechanism.
    -   Specific cross-account impact.
    -   Evidence and a minimal reproduction/test.
    -   Narrow remediation.
5.  **Verified controls** --- protections confirmed in code and tests.
6.  **Coverage gaps** --- paths or scenarios not inspected or not
    testable.
7.  **Prioritized actions** --- fixes ordered by risk and dependency.

Do not inflate severity. Distinguish confirmed vulnerabilities from
design weaknesses, missing tests, and speculative concerns. Do not
report style issues as security findings. Redact secrets and personal
data from evidence.

## Implementation guidance

Prefer patterns such as:

-   An immutable `AccountContext` or equivalent, created only after
    identity and authorization checks.
-   Service methods that require the context explicitly.
-   Repository methods that accept the complete owner scope and include
    it in SQL predicates.
-   Composite constraints that encode actual ownership relationships.
-   Task/event envelopes carrying immutable owner metadata.
-   Account-scoped cache keys and registries.
-   Explicit privileged APIs for intentional cross-account operations.
-   Negative tests that attempt cross-account access using valid
    identifiers from another account.

These are design patterns, not mandates to introduce new abstractions
everywhere. Follow the project's existing conventions when they provide
equivalent guarantees.

## Agent behavior and constraints

-   Inspect the current repository before proposing code changes. Do not
    assume file names, classes, schema, or architecture from this
    document.
-   Map the entry points and data flow before editing
    isolation-sensitive code.
-   Read the relevant callers, callees, schema, migrations, tests, and
    lifecycle paths; do not audit a single function in isolation.
-   Make the smallest change that establishes or restores the ownership
    invariant.
-   Do not silently change the ownership meaning of existing data or
    sentinel values.
-   Do not introduce a new database, framework, RLS layer, process
    model, or dependency without demonstrating why the current
    architecture cannot enforce the requirement.
-   Do not weaken authorization or skip tests to make a change pass.
-   Do not use production credentials or real user accounts for tests
    unless explicitly authorized and safely controlled.
-   Do not expose secrets in generated reports, terminal output,
    screenshots, or test fixtures.
-   Run relevant tests and static checks; report commands and actual
    results. Never claim a test passed if it was not run.
-   If the repository or environment prevents verification, state
    exactly what could not be verified.
-   Ask for clarification only when ownership semantics or an intended
    cross-account operation cannot be determined from code or
    requirements.

## Definition of done

An isolation-sensitive change is complete only when:

-   The resource's owner and allowed scope are explicit.
-   Every reachable entry point supplies validated account context.
-   Reads, writes, deletes, lists, and side effects enforce ownership.
-   Shared runtime state, events, queues, caches, and credentials
    preserve the boundary.
-   Missing or conflicting context fails closed.
-   Negative cross-account tests and relevant positive tests pass.
-   Concurrency and lifecycle behavior are covered where applicable.
-   Migrations/recovery preserve ownership and do not guess ambiguous
    owners.
-   The final report separates verified facts, findings, and unverified
    areas.
