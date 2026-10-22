# Module `core.errors`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\errors.py`

```
Domain errors for the whole framework.

Grouping rules:
  * FlowBlocked family — a plugin flow for ONE account hit a dead end; the
    account's flow aborts with a human reason while everything else runs on.
  * ServerRateLimit    — Discord itself said WAIT (429 / Retry-After); raised
    by adapters, consumed by the scheduler which reports it to the limiter.
  * Config/validation  — ValueError subclasses with precise, human messages;
    both interfaces render them as plain 'error: ...' lines / 400 responses.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `FlowBlocked` | exception | yes | A selfbot flow cannot continue, with a reason. |
| `CacheInvalid` | class | yes | The cached state needed for this action is gone or stale. |
| `TargetGone` | class | yes | The cached channel/target no longer exists (deleted, evicted). |
| `AccessDenied` | class | yes | Discord refused access (permissions revoked, banned from channel). |
| `FetchRequired` | class | yes | The target is not in cache and fetching is disabled/unavailable. |
| `ServerRateLimit` | exception | yes | Discord's own rate limit said WAIT (429 Retry-After / global). |
| `AccountError` | exception | yes | Invalid account configuration or invalid account state. |
| `PluginError` | exception | yes | Plugin loading/lifecycle problem (bad module, duplicate name...). |
| `ProfileError` | exception | yes | Invalid profile definition or assignment. |
| `SyncGroupError` | exception | yes | Invalid sync group definition or membership operation. |
| `SettingsValidationError` | exception | yes | A setting value failed schema validation (path included). |
| `RatePolicyError` | exception | yes | Malformed rate policy (bad interval/jitter/level/owner). |
| `UnsafePolicyError` | exception | yes | A policy would break the minimum-pacing safety floor (1.5s/action). |
| `SchedulerError` | exception | yes | Invalid task/scheduler operation (unknown task, scheduler stopped...). |
| `DatabaseError` | exception | yes | Persistence-layer failure or invalid database operation. |
| `CredentialsError` | exception | yes | A required credential is missing or malformed. |
| `SimulationError` | exception | yes | Scenario/runner problem (unknown scenario, broken script). |

### `FlowBlocked`


### `ServerRateLimit`


### `SettingsValidationError`


## Functions

- `def flow_block_reason(exc: BaseException) -> str`
  - One-line human reason for a flow abort.
