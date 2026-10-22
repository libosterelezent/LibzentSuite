# Module `core.task_scheduler.tasks`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\task_scheduler\tasks.py`

```
Task model for the centralized scheduler (spec §6, INTEGRATION 'core/task_scheduler').

A Task is ONE outbound action for ONE account: plugin X wants to send action
Y (usually 'message' or 'dm'). The scheduler owns the Task's lifecycle and
mutates its status/timing fields as the task moves through the pipeline:

    queued -> running -> completed | failed | cancelled | timed_out

Design notes:
  * ``attempts`` counts RETRIES USED (incremented only when a retry is
    scheduled, not on the final failure). First-try success therefore leaves
    ``attempts == 0`` and a task that succeeded on its second try has
    ``attempts == 1`` (one retry used, two sends total). This makes the
    audit-note contract ``attempts={attempts + 1}`` render the total number
    of sends, and keeps the retry budget at ``1 + max_retries`` tries.
  * ``cancelled`` is the cooperative cancel flag. ``cancel()`` sets it and
    the account worker aborts at its next checkpoint; a lane parked inside
    ``limiter.acquire`` is preempted via ``cancel_event`` (the acquire is
    raced against it and aborted), so a cancel never waits out a pacing
    cooldown.
  * ``as_dict()`` is the JSON-safe snapshot used for task listings, the
    history repository row, and stats — floats are rounded.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `Task` | dataclass | yes | One scheduled outbound action for one account. |

### `Task`

- `def is_terminal(self) -> bool`
  - True once the task reached a final status (no further transitions).
- `def as_dict(self) -> Dict[str, Any]`
  - JSON-safe snapshot of the task (all floats rounded, no objects).
