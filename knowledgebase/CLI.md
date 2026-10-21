# CLI — the real command surface

Source of truth: [interfaces/cli/commands.py](../interfaces/cli/commands.py)
(the `HELP` registry + strict parsing engine) and
[interfaces/cli/repl.py](../interfaces/cli/repl.py). Every command is a thin
wrapper over `core.api.ControlAPI` — CLI and dashboard share one facade.

---

## 1. Modes

| Mode | Invocation | Behaviour |
|---|---|---|
| REPL | `python3 main.py` (no extra args) | Banner `LibzentSuite control terminal — 'help' for commands, 'quit' to exit.`, prompt `sm> `. `api.shutdown()` and `bye.` run on **every** exit path (quit/exit, EOF, Ctrl-C at the prompt, Ctrl-C mid-command) |
| One-shot | `python3 main.py <command ...>` | Runs a single command line (`run_once`); exit code 0 on success, 1 on error; `quit` prints `bye.` and returns 0. `run_once` does **not** call `api.shutdown()` — the lifecycle is owned by `main.py` |
| Help only | `python3 main.py` with empty extra args | Prints the help text, exit 0 |

`--start-all`, `--dashboard`, `--headless`, `--sim`, `--sim-list` compose
with the above (see [CONFIGURATION.md §8](CONFIGURATION.md#8-entrypoint-flags-where-config-comes-from)).

### Error rendering

* Command failures **never leak tracebacks**: exactly one `error: <message>`
  line per failure.
* Domain errors (`UsageError`, `AccountError`, `PluginError`,
  `ProfileError`, `SettingsValidationError`, `RatePolicyError`,
  `UnsafePolicyError`, `SchedulerError`, `CredentialsError`, `ValueError`,
  `RuntimeError`) render bare (`error: <msg>`); anything else keeps the
  exception type name (`error: TypeError: ...`) so bugs stay identifiable.
* Errors never exit the REPL. The help footer states this:
  `errors print as one 'error: ...' line and never exit the REPL; 'quit' shuts everything down first`.

---

## 2. Command reference (verbatim from the `HELP` registry)

### Top level

| Command | Usage | Description |
|---|---|---|
| `help` | `help` | show this help |
| `status` | `status` | overview: accounts, tasks, rate limits, warnings |
| `estop` | `estop [resume]` | GLOBAL emergency stop; `estop resume` re-arms after one (`unhalt` also accepted) |
| `quit` | `quit` | graceful shutdown, then exit (alias: `exit`) |
| `logs` | `logs [n] [account]` | structured log tail (default: last 20) |
| `audit` | `audit [n] [account]` | interaction audit trail (default: last 20) |
| `simulation` | see below | offline scenario runs |

### `accounts` — account management

| Usage | Description |
|---|---|
| `accounts list` | list every account (status, discord id, enabled flag, assigned plugins) |
| `accounts add <name> [plugin ...]` | register + persist an account; the optional plugin names become its DIRECT assignments (payload `{name, plugins}` — a `profiles` key is never sent; the backend rejects it) |
| `accounts plugins <account>` | the account's assigned plugins (sorted; "no plugins assigned" when empty) |
| `accounts plugin-add <account> <plugin>` | assign one plugin (idempotent; applies IMMEDIATELY — the account's plugins re-attach, no restart needed) |
| `accounts plugin-remove <account> <plugin>` | remove one assignment (idempotent — an already-absent assignment renders "was not assigned … nothing to do", never an error) |
| `accounts remove <name>` | remove an account |
| `accounts info <name>` | the account details page (Status / Identity / Runtime client-cache / Plugins / Profiles (informational) / Rate Limit) |
| `accounts enable <name> <on\|off>` | enable/disable (also accepts `true`/`false`) |
| `accounts start [name\|all]` | connect one account or every enabled account |
| `accounts stop [name\|all]` | stop one account or all |
| `accounts estop <name>` | emergency-stop one account |

Errors: unknown account → `AccountError` ("unknown account …"); unknown
plugin → `ValueError` listing the known plugins — both render as one
`error:` line, exit code 1 in one-shot mode.

### `plugins` — plugin management

| Usage | Description |
|---|---|
| `plugins list` | manifests: name, version, description + settings schema with bounds |
| `plugins state <account>` | per-account state: `[+]` enabled / `[-]` disabled, resolved settings, error counters |
| `plugins enable <account> <plugin>` | enable for that account |
| `plugins disable <account> <plugin>` | disable for that account |
| `plugins settings <account>` | settings-only view (`[*]` — payload carries no enabled flag) |
| `plugins set <account> <plugin> <key> <value>` | validate + persist one account-layer setting (live) |

### `profiles` — profile records (RETAINED FUTURE INFRASTRUCTURE)

> Profiles do **not** control which plugins an account runs — capabilities
> are direct account→plugin assignments (`accounts plugin-add` /
> `plugin-remove`). The commands below are bookkeeping over the retained
> profile tables (future: plugin-specific configurations).

| Usage | Description |
|---|---|
| `profiles list` | name, plugin list, description |
| `profiles create <name> <plugin ...>` | define a record (needs ≥ 1 plugin) |
| `profiles delete <name>` | delete it |
| `profiles assign <account> <profile>` | bookkeeping grant (NO capability effect — plugins do not re-attach) |
| `profiles unassign <account> <profile>` | bookkeeping revoke (no capability effect) |

### `ratelimit` — rate policies

| Usage | Description |
|---|---|
| `ratelimit show` | stored policies table (scope/action/policy) |
| `ratelimit stats` | engine snapshot: floor, policies, active cooldowns, 429 telemetry (`hits_429` per account + fleet total), grant/wait counters |
| `ratelimit set <action> <interval> [jitter_min jitter_max] [level owner]` | set a policy (2, 4 or 6 args exactly) |
| `ratelimit clear <level> <owner> <action>` | remove one (`-` or `none` as the owner placeholder for level `global`) |
| `ratelimit effective <action> [account] [plugin]` | effective pacing + the source stack (max wins, marked `<- pacing source`) |
| `ratelimit warnings` | safety warnings (`[i]` info / `[!]` warning / `[X]` danger) |
| `ratelimit floor <seconds>` | raise the safety floor (raise-only; can never drop below the hard minimum) |
| `ratelimit lane [on [rate]\|off\|rate <s>]` | **REMOVED feature stub**: the fleet send lane was removed (sync groups + per-account pacing replace it); the subcommand answers the compat stub and mutations are ignored |

Levels: `global|account|plugin|account_plugin`; `account_plugin` owner must
be `<account>/<plugin>`; `global` takes no owner, every other level needs
one. When NO policy is stored for an action the engine falls back to the
LEARNED defaults (message 2.6 s, dm 2.6 s, reaction 2.5 s, component
3.0 s — measured zero-throttle rates; see
[RATE_LIMITING.md](RATE_LIMITING.md)).

> **Not CLI commands:** `prefixrate`, `adminlist`, `adminperms` and
> `adminprefix` are Discord **self-command** verbs (typed with the
> account's prefix in Discord — see [API_ENDPOINTS.md](API_ENDPOINTS.md)
> "Plugin self-command surface"). The admin panel is dashboard-driven
> (`/api/admin/*`); per-account send pacing from the shell is
> `ratelimit set message <s> account <name>`.

### `tasks` — task queue + history

| Usage | Description |
|---|---|
| `tasks list [status]` | queued/running tasks |
| `tasks cancel <id>` | cancel one task (id may be negative — no bound check) |
| `tasks history [n]` | finished tasks (default 50; outcome + duration) |
| `tasks cancel-matching [account] [plugin]` | bulk cancel |

### `simulation` — offline scenario runs

| Usage | Description |
|---|---|
| `simulation list` | available scenarios (or `(no scenarios available)`) |
| `simulation run [name]` | run one; **without a name raises `UsageError` listing the available scenarios** |

---

## 3. Strict parsing rules

* **shlex parsing** — quoted arguments work; unclosed quotes raise
  `UsageError: cannot parse command (check your quotes): …`.
* **Exact argument counts** — enforced per subcommand, e.g.
  `accounts remove: expects 1 argument(s), got 2 — usage: accounts remove <name>`.
  Missing/unknown subcommands name the group's full usage line.
* **Special counts**: `ratelimit set` accepts exactly 2, 4 or 6 arguments;
  `ratelimit effective` 1–3; `tasks list`/`history` ≤ 1 extra;
  `tasks cancel-matching` ≤ 2; `accounts add` ≥ 1; `profiles create` ≥ 2;
  `accounts plugins` exactly 1; `accounts plugin-add`/`plugin-remove`
  exactly 2.
* **Booleans** — `on|true` / `off|false` only (case-insensitive); anything
  else: `accounts enable: expected <on|off>, got 'maybe'`.
* **Numbers** — `float(token)` / `int(token)` with contextual errors:
  `ratelimit set: interval 'fast' is not a number — usage: …`; integers must
  be non-negative unless explicitly allowed (`tasks cancel <id>`).
* **Level/owner pairs** — validated by `_check_level_owner` (see above);
  `ratelimit clear` maps `-`/`none` → owner `None`.
* **`logs`/`audit` tails** — optional leading integer then optional account;
  `n` must be a non-negative integer.
* Unknown commands: `unknown command 'foo' — try 'help'` (not an error).

---

## 4. Examples per group

```text
sm> status
sm> accounts add main soulcopy roll
sm> accounts plugins main
sm> accounts plugin-add main roll
sm> accounts plugin-remove main soulcopy
sm> accounts enable main on
sm> accounts start all
sm> accounts info main
sm> accounts estop main

sm> plugins list
sm> plugins state main
sm> plugins set main soulcopy inter_chunk_delay 12.5

sm> profiles create grinders soulcopy roll
sm> profiles assign main grinders    (bookkeeping only — no capability effect)

sm> ratelimit show
sm> ratelimit stats
sm> ratelimit set message 2.0 0.1 0.5
sm> ratelimit set message 4.0 plugin roll
sm> ratelimit set dm 3.0 account main
sm> ratelimit clear account main dm
sm> ratelimit effective message main soulcopy
sm> ratelimit floor 2.0
sm> ratelimit lane on 1.5       # lane REMOVED — stub reply, setting ignored

sm> tasks list running
sm> tasks cancel 17
sm> tasks history 10
sm> tasks cancel-matching main soulcopy

sm> simulation list
sm> simulation run soulcopy_basic

sm> logs 50 main
sm> audit 20
sm> estop
sm> estop resume
sm> quit
```

One-shot equivalents:

```bash
python3 main.py accounts list
python3 main.py ratelimit effective message
python3 main.py estop resume
```

Related: the dashboard mirrors the same operations over REST/WS —
[DASHBOARD.md](DASHBOARD.md). Settings reference: [CONFIGURATION.md](CONFIGURATION.md).
