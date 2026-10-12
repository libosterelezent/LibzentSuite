# Module `adapters.simulator.runner`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `adapters/simulator/runner.py`

```
SimulationRunner — a COMPLETE ephemeral stack per scenario run (spec §16).

Wiring follows the pinned constructors in INTEGRATION.md exactly (async
boot order, same as the production bootstrap)::

    await Database(":memory:").connect()
    CredentialService(db.credentials, key_path=<TEMP KEY PER RUN>) /
        CooldownService(db.cooldowns) / PluginStateService(db.kv)
        <- synthetic creds, restore()d
    EventBus()
    LogService() + await start_persistence(db.logs)   (queued db sink)
    SettingsManager(bus, logger, plugin_repo=db.plugins,
                    account_settings_repo=db.account_settings,
                    global_layer=DEMO_PLUGIN_SETTINGS) + await load()
    RateLimitManager(bus, logger, db.policies, kv_repo=db.kv,
                     lane_enabled=False, lane_prefixrate=1.5)
        + await load_persisted()
    AuditRecorder(db.audit) + await start_persistence(db.audit)
    TaskScheduler(bus, logger, limiter, sender)    <- LATE-BOUND SENDER (below)
        + await restore_sequence()
    PluginManager(bus, logger, settings, db.plugins, plugin_dir)
        + discover() + await persist_metadata()
    ProfileManager(db.profiles, logger, db.accounts) + await load()
    AccountManager(bus, logger, db.accounts, plugins_mgr, profiles, scheduler,
                   settings, audit, client_factory, credentials[, limiter],
                   credential_service=..., cooldowns=..., plugin_state=...)
        + await load()

HERMETICITY (audit round 3): the ephemeral CredentialService gets a TEMP
key file per run (``tempfile.mkdtemp``, removed in teardown) — the default
key path is the CWD-relative ``state/credentials.key``, so sim runs used
to lazily CREATE/read the PRODUCTION master key.

LANE WIRING (audit round 3): the RateLimitManager is built with the kv
repo and explicit lane args like production, but the lane is DISABLED —
a disabled lane is a 0s pass-through, so scenario pacing is unchanged.
Scenarios can flip it live through ``ScenarioAPI.limiter``
(set_lane_enabled / set_lane_prefixrate); before this the lane machinery
was absent from every sim run and lane pause was dead code in sims.

TEARDOWN ORDER (audit round 3): matches Framework.shutdown — remote
control, scheduler drain (queued tasks complete while the account can
still send), bus drain, accounts stop, log/audit persistence stops,
bridge detach, plugin close, database close — plus the sim-only cleanups
(sys.modules purge of this run's plugin modules, temp key removal).

RESIDUAL GAP (documented, deliberate): production's
``_wire_event_logging`` (bus lifecycle events -> structured 'events' log
lines) is bootstrap-only; sim reports therefore lack TaskCompleted /
RateLimitTriggered / similar event lines. Wiring it here would duplicate
the bootstrap's process-wide subscription semantics for little scenario
value — scenarios assert on the audit trail and world journal instead.

THE LATE-BOUND SENDER SEAM (documented for the integrator): the scheduler
needs the account manager's ``send_hook`` as its sender, but the account
manager needs the scheduler in its own constructor. The cycle is broken with
a late-bound holder — ``sender = lambda task: box["send"](task)`` — where
``box["send"]`` is filled with ``accounts.send_hook`` immediately after the
AccountManager exists (before ``scheduler.start()``, so no task can ever run
against an unfilled box).

Manager modules from A3/A5/A6 are imported INSIDE run(): when one is still
pending integration, run() raises SimulationError with a human-readable
message instead of an ImportError traceback.

Documented contract decisions (ambiguous seams — see INTEGRATION.md
ground rules; the lead reconciles at integration):

- LOGGER SHAPE: the spine SettingsManager and the A3/A6 managers call the
    injected logger in STDLIB style (``logger.info("fmt %s", arg)``,
    ``logger.exception(...)``), while A4's rate-limit engine calls it in
    LogService style (``logger.info(source, message)``) and A5's scheduler
    auto-detects. This runner therefore injects :class:`ServiceLogger` — a
    tiny stdlib-logger-shaped adapter over ``LogService.record`` — into every
    manager. Both call styles produce a readable structured entry (so
    ``log_lines`` and the dashboard Logs page keep working), and a logging
    failure can never break a manager.

- ACCOUNT MANAGER ``limiter`` KEYWORD: passed when accepted (A7 assignment
  wording); the INTEGRATION.md pinned signature omits it, so a TypeError
  falls back to the keyword-less construction — either variant works.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `SimulationRunner` | class | yes | Builds and tears down a full in-memory stack for each scenario run. |

### `SimulationRunner`

- `def list(self) -> List[dict]`
  - [{name, description}] for every registered scenario.
- `def scenario(self, name: str) -> Scenario`
  - Scenario lookup; raises SimulationError for unknown names.
- `async def run(self, name: str) -> dict` — side effects: `creates_tasks, publishes_event, db_write, network`
  - Run one scenario on a fresh ephemeral stack; returns the report.

## Functions

