# Module `core.remote_control`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/remote_control.py`

```
Remote control: manage the framework FROM Discord via mention prefixes.

An owner types ``@<selfbot account> <command>`` in any channel the account
can see; this processor picks it up, runs the command through the ONE
CoreAPI (same path as CLI/dashboard — parity by construction) and replies in
the same channel through the FULL send pipeline (scheduler -> lane ->
pacing -> jitter -> audit), so management traffic is rate-safe by design.

Safety model:
  * OWNER_USER_IDS only — anyone else's mention is ignored completely
    (no reply, no log noise beyond a debug line),
  * an explicit allowlist of read-mostly + safe commands; nothing here can
    change secrets or talk to Discord beyond the reply itself,
  * replies are truncated, self-throttled per account (reply_cooldown_s),
    and submitted at NORMAL priority through the paced pipeline,
  * opt-out: set ``REMOTE_CONTROL=0`` in .env to disable wiring entirely.

Mention syntax: the message must START with the account's own mention —
``<@user_id>`` or ``<@!user_id>`` (the bang form older clients emit) —
followed by whitespace and the command line. Each account answers only to
ITS OWN mention, so ``@alpha status`` and ``@beta status`` are independent.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `RemoteControl` | class | yes | Mention-prefix command bridge: Discord messages -> ControlAPI -> reply. |

### `RemoteControl`

- `def start(self) -> 'RemoteControl'`
  - Subscribe to inbound messages (idempotent).
- `def stop(self) -> None`
  - Unsubscribe; in-flight handlers finish, no new ones start.
- `def forget_account(self, account: str) -> None`
  - Drop an account's reply-throttle stamp (removal cascade).
