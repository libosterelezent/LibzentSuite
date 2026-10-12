# Implementation / Documentation Discrepancies

> Recorded during the knowledge-base build (4-agent investigation, commit
> `35176a8` baseline). Per the source-of-truth rule: **the implementation
> is the actual behavior**; every entry below is a documented divergence,
> not a hidden rewrite. Items marked ✅ were corrected during this pass
> (pure documentation fixes — no behavior changes); the rest are open.

## Open — behavioral / structural

1. **REPL-path shutdown order** — `Framework.shutdown` documents "detach
   logging sinks BEFORE `database.close()`", but `ControlAPI.shutdown()`
   (called by `Repl.run` on every exit path) closes the database without
   detaching the sinks and without stopping `remote_control`;
   `main.run_app`'s `finally` then calls `framework.shutdown()` against an
   already-closed DB. Degraded-but-safe (`db_failures` counter absorbs
   it), but the documented guarantee does not hold on REPL quits.
2. **`PluginServices` protocol gaps** — the protocol class does not
   declare `add_reaction`, `click_button`, `fetch_user` (all implemented
   by `AccountPluginServices` and part of the documented PUBLIC plugin
   API). The protocol should be reconciled.
3. **`send_*` return type** — protocol pins `-> DiscordMessage`;
   implementation returns **`None`** on success (fire-and-await-ack).
   Consequence: `plugins/roll` `confirm_reply` branch is dead code.
4. **Event envelopes are mutable** — the rich DTOs are frozen, but the bus
   envelopes (`MessageReceived` etc.) are plain dataclasses shared across
   all plugins; read-only is convention, not enforced.
5. **`ACCOUNT_SETTINGS` not editable from any interface** — the settings
   comment claims "editable from both interfaces", but nothing routes
   `SettingsManager.set_account_setting()` (CLI, dashboard, ControlAPI
   all lack it; `accounts info` doesn't display them either).
6. **`SettingsManager.layers_view()` dead code** — no route/command calls it.
7. **`ControlAPI.credentials_summary()` unrouted** — documented in the
   method map, served by neither surface.
8. **`--tui` ghost flag** — advertised in `main.py`'s docstring; argparse
   doesn't define it (falls through to one-shot mode as an unknown
   command).
9. **core→adapters runtime import** — `core/account_manager/manager.py`
   imports `adapters.base.DiscordMessage` at module level (runtime),
   while `event_bus`/`plugin_base` deliberately keep the boundary
   `Any`/TYPE_CHECKING-only. Dependency-light, but an architecture edge.
10. **Lane stage missing from limiter docstrings** — `Grant.reason` also
    takes `"lane"` (stage 0), but the `Grant` docstring, `acquire()`'s
    stage list, and the `RateLimitTriggered.reason` field comment all say
    only `"server"` / `"pacing"`. Docstrings predate the lane feature.
11. **`handle_event` docstring + `_dispatch` annotation** — say/annotate
    `MessageReceived`, but four event types are routed.
12. **No-production-subscriber events** (informational): `TaskQueued`
    (excluded from `_LOGGED_EVENTS` and dashboard push) and
    `SettingChanged` (emitted, subscribed by nothing in production).
13. **Loader name regex** — `[a-z0-9_-]+` accepts leading dash/underscore,
    laxer than the docstring's intent.
14. **`activate/deactivate_account` pinned sync in INTEGRATION.md**,
    implemented async (self-documented deviation in the module docstring).
15. **`db.py` docstring** says "nine repositories"; the class exposes ten.
16. **Two `DatabaseError` classes** — `core.errors.DatabaseError` and
    `database.connection.DatabaseError` are DISTINCT classes (both
    `RuntimeError` subclasses). Repositories import whichever is nearest:
    `database/repositories/accounts.py` raises the `core.errors` flavor,
    while `BaseRepository._tx`/`_fetch_*` wrap unexpected exceptions into
    the `database.connection` flavor — so an `IntegrityError` re-raised as
    one flavor inside `_tx` gets RE-wrapped into the other. Callers using
    `except DatabaseError` with either import mostly work by luck
    (`RuntimeError` ancestry + the double-wrap); `isinstance` checks
    against the wrong flavor silently miss. One should alias the other.
17. **Async-conversion missing-`await` hazards (found by testing, fixed in
    the same pass)** — the async repository layer landed with five latent
    bugs of the same shape, all caught by the rewritten
    `tests/unit/test_database.py`: `AccountsRepository.assign_profile`
    chained `.fetchone()` on aiosqlite's `Result` wrapper (attribute does
    not proxy); `ProfilesRepository.upsert` never awaited
    `conn.executemany` (plugin lists silently never persisted);
    `RatePolicyRepository._validated`/`_owner_key`/`_record_from_row`
    were `async` but called without `await` (every policy write raised
    `AttributeError` on the coroutine); `KvRepository._check_key` was
    `async` and un-awaited (empty-key validation never executed). Pattern
    note for reviewers: `async def` helpers invoked synchronously FAIL
    SILENTLY in Python (a coroutine object is truthy) — grep for
    un-awaited coroutines in any future async conversion.
18. **`tests/unit/test_async_db_services.py` hung the whole suite** — the
    `TestCooldownService` cases never closed their `:memory:` databases;
    aiosqlite's worker thread is NON-daemon, so the interpreter never
    exited (tests all passed, process hung at exit). Fixed by adding the
    closes; recorded because "tests pass but the runner hangs" is a
    confusing failure mode other unclosed-connection tests could
    reintroduce.
19. **Integration tests self-skipped until the async bootstrap landed
    (RESOLVED in-pass)** — `tests/integration/test_account_persistence.py`
    probes the pinned contracts (`bootstrap.build` async, ControlAPI
    credential/cooldown routes, `AccountManager.load`) at import time and
    skipped with reason `"pending agent integration (<missing pieces>)"`
    while any was absent. All five tests now run and pass against the
    landed contracts; the probe remains as a guard for future regressions
    of the pinned surfaces.
20. **`ControlAPI.plugins_set_setting` never awaited the async
    `PluginManager.set_setting`** (found by the restart-matrix integration
    test; fixed in the same pass) — the route returned a dict whose
    `value` was a coroutine object and NOTHING persisted. Same
    missing-await family as entry 17; noted separately because it sat in
    the API layer, not the repository layer.
21. **`credential_source` displays `missing` for `.env`-backed accounts
    until first start** — `CredentialService.status()` can only report
    `env` after a `resolve(..., legacy_env_lookup=...)` has run (the
    runtime-source map is populated at resolution, and the manager
    resolves at `start`). Display-only: resolution precedence itself is
    correct, but a freshly added account backed solely by `DISCORD_TOKEN_X`
    shows `missing` in `accounts_credential_status` until started (or
    otherwise resolved). A status probe that consults the legacy lookup
    without storing would fix the display.
22. **`Framework` attribute naming vs the pinned protocol** — the pinned
    contract names the ControlAPI kwargs `credentials_svc`/`cooldowns`,
    and ControlAPI indeed takes those; but the `Framework` container
    exposes them as `credential_service`/`cooldowns`/`plugin_state`
    (no `credentials_svc` attribute). Consumers must use the container
    names; the near-miss naming is recorded so integrators don't assume
    symmetry.

23. **`require_json_list` is called but never defined** —
    `interfaces/dashboard/server.py` routes `POST /api/admin/ids/set`
    (line ~968) and `POST /api/admin/perms/set` (line ~988) call
    `require_json_list(body, ...)`, which is defined nowhere and imported
    from nowhere (verified by AST: no binding of that name exists in the
    module). Both routes therefore fail with a `NameError` that the
    unexpected-error path converts to a 500. The shipped Admin tab only
    uses the working `ids/add`/`ids/remove`/`perms/add`/`perms/remove`
    forms, so the bug is latent. Fix: define the helper (validate the
    field is a list of ints) or reuse the inline validation pattern of
    `/api/accounts/plugins/set`.

## Corrected during this pass ✅

- Root `README.md`/`ARCHITECTURE.md` stale test count (450 → actual 550)
  and stale scenario list (missing `control_mention`).
- `requirements.txt` stale module path (`core/discord_adapter.py` →
  `adapters/discord/client.py`) and dangling KB_07 reference.
- `scripts/gen_knowledgebase.py` docstring claimed it writes
  `read_only/README.md` (that file is handwritten).
- `tests/simulation/test_scenarios.py` docstring still described skips
  that no longer exist.
- Docstring RST defects that broke the Sphinx `-W` build (indentation,
  unbalanced emphasis, literal-block markers) in `adapters/base.py`,
  `adapters/simulator/{runner,world}.py`, `core/audit.py`,
  `core/config.py`, `core/rate_limit_manager/engine.py`,
  `core/task_scheduler/scheduler.py`, `core/task_scheduler/tasks.py`,
  `adapters/model.py`, `interfaces/dashboard/ws.py`, `main.py`.

## How to report new discrepancies

Open an entry here (module, expectation, actual behavior, evidence) —
never edit the generated `read_only/` snapshot or silently rewrite docs
to match code without recording it.
