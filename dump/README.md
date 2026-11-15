# dump/ — the retirement wing

Stale, dead, or replaced code lives here — **preserved verbatim**
(`git mv` keeps history; nothing is rewritten), out of the production
tree, out of every tool's scope (mypy, ruff, coverage, CI).

## Why a dump and not deletion

Retirements stay reviewable: every entry below says what it was, when
it moved, what replaced it, and how to bring it back. If something
retired too eagerly, restoring is one `git mv` plus re-adding whatever
wiring note the entry lists.

## Inventory

| Path (under dump/) | Moved | Was | Replaced by |
|---|---|---|---|
| `core/remote_control.py` + `tests/test_remote_control.py` | 2026-11-15 | The mention-prefix RemoteControl bridge + its tests | FrameworkVerbsCog carries the verbs library-natively; the bridge had no production dispatcher left |
| `core/intake_cog.py` + `tests/test_intake_cog.py` | 2026-11-15 | The C1 intake mixin (test-only constructors) | The adapter wires its own event handlers directly |
| `tests/test_plugin_commands.py` + `tests/test_plugin_commands_e2e.py` | 2026-11-15 | The hub registry/executor tests | (retired with the dispatch path) |
| `serve_dashboard.sh` | 2026-11-15 | Launcher for deleted `--dashboard` flags | The FastAPI surface (unwired, interfaces/api/) |
| `fragments/commands_hub_machinery.py` | 2026-11-15 | The hub's spec table, handlers, help renderer, PacingBackend, dead members | The lean hub (bind/unbind + api_provider/access) + FrameworkVerbsCog |
| `fragments/manager_self_command_dispatch.py` | 2026-11-15 | execute_self_command, _find_command, command_entries, _settings_api, _sync_commands | The cog command path |
| `fragments/plugin_base_command_grammar.py` | 2026-11-15 | CommandSpec/ArgSpec/CommandContext/parse_command_args/usage_line/CommandSettingsApi | COG typed params (the C2 flip completed) |
| `fragments/ping_commands_face.py` + `fragments/test_ping_commands_face.py` | 2026-11-15 | ping's COMMANDS face + its tests | The behavior (_ping) stays; the command returns as a COG when the host injects the paced facade (roadmap) |
| `fragments/scenarios_control_mention.py` | 2026-11-15 | The sim's control_mention scenario + ScenarioAPI.remote | The estop scenario drives ControlAPI.emergency_stop_all directly |
| `fragments/api_dashboard_surface.py` | 2026-11-15 | dashboard_ui_page/_ui_account_context/dashboard_token from the facade | (the dashboard retired 2026-11-13; these could only return None) |
| `fragments/credentials_remote_control_enabled.py` | 2026-11-15 | The REMOTE_CONTROL env gate | (its only reader was the bridge) |
| `fragments/test_bootstrap_remote_control_matrix.py`, `test_round3b_remote_control_mask.py` | 2026-11-15 | The gate's tests / the bridge's masking-contract test | (retired with their subjects) |
| `fragments/cog_host_intake_mixin.py` | 2026-11-15 | The FrameworkIntakeCog mixin from cog_host | (the adapter's own handlers) |
| `fragments/names_group_name_re.py` | 2026-11-15 | The sync-group name validator | The workspace unification (W5) |

## Restoration

`git mv dump/<path> <original-path>` and re-wire the call sites the
entry lists. Everything here is byte-identical to its last production
state — history (`git log --follow`) carries the full context.
