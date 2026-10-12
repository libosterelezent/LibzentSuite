# Module `interfaces.cli.commands`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `interfaces/cli/commands.py`

```
Shared CLI command engine (spec §13) — used by the REPL and one-shot mode.

Every command is a thin, STRICTLY-validated wrapper over ``core.api.ControlAPI``
(the only object this module ever touches; duck-typed, so tests inject a fake).
Query commands render through the pure ``format_*`` helpers at the bottom so
payload rendering is unit-testable without a live stack.

Behaviour pinned here:
  * ``run_command(api, line)`` shlex-parses ``line`` and returns display text
    (``None`` for an empty line). It raises ``QuitRequested`` for quit/exit and
    ``UsageError`` for bad usage; every other exception (domain errors like
    ``AccountError``/``UnsafePolicyError`` included) bubbles for the caller's
    error renderer — this engine never swallows failures.
  * Argument counts are validated exactly, with contextual usage strings.

Decisions for things INTEGRATION.md left open (documented per the contract):
  * ``logs``/``audit`` default to the last 20 entries (the old CLI default).
  * ``ratelimit clear`` always takes 3 arguments; the owner placeholder for
    level ``global`` is ``-`` or ``none`` (passed as ``owner=None``).
  * ``simulation run`` without a name raises ``UsageError`` listing the
    available scenarios instead of guessing one.
  * ``accounts add <name> [plugin ...]`` assigns plugins DIRECTLY (payload
    ``{"name": ..., "plugins": [...]}``; the key is omitted when none are
    given). Profile-mediated capabilities were removed from the backend —
    ``profiles`` is never sent (it is rejected as an unknown payload key).
  * ``accounts plugins|plugin-add|plugin-remove`` manage the direct
    account→plugin assignments through the facade; removal is idempotent
    (``existed=false`` renders a "nothing to do" message, never an error).
    Assignments apply immediately — there are no next-start semantics.
  * The ``profiles`` group stays (retained future infrastructure: profile
    CRUD + bookkeeping assignments), but its help text says plainly that
    profiles do NOT control which plugins an account runs.
  * ``accounts enable <name> <on|off>`` also accepts true/false; a real bool
    is passed down (ControlAPI.parse_bool re-validates on its side anyway).
  * ``accounts credential <name> <token>`` prints a shell-history warning:
    values are stored encrypted, but the CLI argument itself may linger in
    the shell's history — the dashboard dialog is the preferred entry.
  * ``credentials import-env`` accepts exactly one flag, ``--overwrite``
    (anything else is a usage error, never a silent ignore).
  * ``cooldowns`` with no argument lists every account's cooldowns;
    ``cooldowns clear`` without a key clears ALL cooldowns of that plugin
    on that account.
  * Mutation results are rendered generically (``key=value`` pairs) from the
    JSON payload the facade returns, keeping CLI/dashboard parity by
    construction.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `QuitRequested` | exception | yes | Raised by ``run_command`` for quit/exit (the REPL shuts down cleanly). |
| `UsageError` | exception | yes | Raised when a command line is malformed (bad arg count / bad number). |

## Functions

- `def help_text() -> str`
  - Render the HELP registry as aligned text (with an error footer).
- `async def run_command(api: Any, line: str) -> Optional[str]`
  - Execute one command line against the ControlAPI.
- `def format_status(status: dict) -> str`
  - Overview payload (api.status) -> aligned text: uptime/tasks/accounts/
- `def format_accounts(accounts: List[dict]) -> str`
  - Account list payload (api.accounts_list) -> one block per account.
- `def format_account_info(details: dict) -> str`
  - Account details payload (api.accounts_info) -> the spec §8 page.
- `def format_account_plugins(account: str, plugins: Sequence[str]) -> str`
  - Assigned-plugin list (api.accounts_plugins_list) -> sorted lines.
- `def format_plugins(manifests: List[dict]) -> str`
  - Plugin manifests (api.plugins_manifests) -> metadata + settings schema.
- `def format_plugin_state(states: List[dict]) -> str`
  - Per-account plugin state (api.plugins_state / plugins_settings).
- `def format_profiles(profiles: List[dict]) -> str`
  - Profiles payload (api.profiles_list) -> name, plugins, description.
- `def format_groups(groups: List[dict]) -> str`
  - Sync groups payload (api.groups_list) -> name, members, description.
- `def format_group_snapshot(snapshot: dict) -> str`
  - Group snapshot (api.groups_snapshot) -> the full group page.
- `def format_lane(lane: dict) -> str`
  - Lane state -> display lines (sequential sends: spacing + pause flag).
- `def format_policies(policies: List[dict]) -> str`
  - Stored policies (api.ratelimit_policies) -> scope/action/policy table.
- `def format_effective(effective: dict) -> str`
  - Effective pacing (api.ratelimit_effective) -> interval + source stack.
- `def format_tasks(tasks: List[dict]) -> str`
  - Queued/running tasks (api.tasks_list) -> a compact table.
- `def format_history(tasks: List[dict]) -> str`
  - Finished tasks (api.tasks_history) -> outcome + duration per task.
- `def format_logs(entries: List[dict]) -> str`
  - Structured log entries (api.logs_tail) -> 'HH:MM:SS SEVERITY source msg'.
- `def format_audit(entries: List[dict]) -> str`
  - Audit entries (api.audit_tail) -> compact interaction lines.
- `def format_warnings(warnings: List[dict]) -> str`
  - Safety warnings (api.ratelimit_warnings) -> icon lines.
- `def format_simulation_report(report: dict) -> str`
  - Simulation report (api.simulation_run) -> verdict + first 30 log lines.
- `def format_credential_status(rows: List[dict]) -> str`
  - Credential status payload (api.accounts_credential_status) -> table.
- `def format_import_report(report: dict) -> str`
  - Import report (api.credentials_import_env) -> names-only summary.
- `def format_cooldowns(rows: List[dict]) -> str`
  - Active cooldowns (api.cooldowns_list) -> account/plugin/key table.
