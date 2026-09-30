# TODO — SlaughterMudae

Modular selfbot framework for Mudae automation: plugins with declared
settings, multi-account container with roles, layered rate limiting
(buckets + Discord's own headers + serialized lanes), SQLite persistence,
full CLI/dashboard parity, audit trail, simulations, and a live test suite.

> Architecture: see ARCHITECTURE.md (component map + live/testing adapters:
> Framework → Discord Adapter → user-account client for live; Framework →
> Simulation Adapter for offline runs/tests).

---

## v2 — Every feature with settings accessible via BOTH APIs (CLI + Dashboard)  ✅ DONE

- [x] Single shared command layer: every mutation goes through `ControlAPI`;
      CLI (`cli/commands.py`) and dashboard routes are 1:1 wrappers (KB_19
      parity table)
- [x] **Plugin settings page**: dashboard details modal with per-plugin setting
      editors (`POST /api/setting`); CLI `settings <acct>` + `set <acct>
      <plugin> <key> <value>` — validated via SettingDef, persisted as the
      account layer, immediate runtime effect
- [x] **Account add/remove handling**: CLI `add-account`/`remove-account`,
      dashboard forms; persisted in SQLite; cascade cleanup of states
- [x] **Account list with names AND ids under them** (CLI `accounts`,
      dashboard cards — id shown beneath each name once running)
- [x] **Details button beside each account** → modal (CLI `details <acct>`)
      showing in-cached-memory data ONLY (discord.py-self client caches:
      user, guilds, channels, DM channels) — no extra fetching beyond what
      the library already holds (KB_19)
- [x] **Database for persistency and secrecy** (KB_15): SQLite `state/slaughter.db`,
      chmod 0600, accounts/plugin states/settings/limits/delays/roles/audit;
      token VALUES stay in `.env` only; one-time migration from the old JSON store
- [x] **Roles system** (KB_16): `Role` = named bundle of permitted plugins;
      assign/unassign per selfbot (CLI + dashboard + DB); union gating at
      account start; future `Roll`/`Snipe` plugins just get referenced by roles
- [x] **Rate limit settings page per selfbot or global + Advanced settings**
      (KB_17): human-like random delay ranges (`x.xx..y.yy` seconds) applied
      after every grant — global default or per-bot override, settable from
      either surface, chosen from the account list
- [x] **Live simulation test suite with detailed logging view** (KB_18):
      audit trail of every Discord interaction (waits + delays per send,
      inbound events) streamed to the dashboard Audit page and stored in the
      DB; `scripts/sim_inspect.py` runs audited scenarios locally with a
      summary + safety review (`--json` for diffs)
- [x] **Dangerous-settings warnings** (KB_17): banner (dashboard) + `warnings`
      (CLI) for aggressive rules / missing or narrow delays / no jitter;
      HARD FLOOR — no rule below 1.5s/action can be set from any surface
      (`UnsafeRateRuleError` with explanation)

## v1 — carried over, all complete

- [x] Phase 0: scaffolding, stdlib-only core, validated config formats
- [x] Phase 1: plugin architecture (manifests, 3-layer settings, loader,
      lifecycle, error isolation, soulcopy migration)
- [x] Phase 2: multi-account container (sync groups, account×plugin matrix,
      health containment, auto-reconnect with exponential backoff)
- [x] Phase 3: centralized rate limiter (global + per-bot layers, precedence,
      priorities, jitter, live stats, all sends enforced)
- [x] Phase 4: terminal REPL + full-screen TUI; dashboard SPA + WebSocket
      live push; shared ControlAPI
- [x] Phase 5: graceful shutdown, state persistence, README, knowledgebase,
      test suite (169 tests incl. local simulations)

---

## Notes / Risks

- ⚠️ Selfbots violate Discord ToS — suspension risk. The 1.5s/action floor +
      human delay ranges exist to mitigate, not authorize; keep rules conservative.
- Tokens/ids only in `.env` (gitignored, chmod 600); the DB stores refs, never
  token values; tests use synthetic ids.
- `knowledgebase/` is local-only, read-only on disk (`chmod -R a-w`).
- Role changes apply when the account is re-added or on the next process start.
- Future plugins (`roll`, `snipe`) only need to exist under `plugins/` and be
  listed in a role to slot into the role-based control flow.

## Audit rounds

- [x] v2.1 — 15-agent deep audit (module-by-module + adversarial rate-limit
      stress): 16 confirmed bugs fixed, ~35 risks hardened, 30 regression
      tests + permanent 32-test stress suite. Full findings: KB_20.
- [x] v2.2 — 8-agent error-handling audit (UI, HTTP/WS contract, CLI
      messages, backend containment, plugins, fuzzing, live scenarios,
      terminal UX): wrong-action bool coercions eliminated, transactional
      add/remove, sane field-level messages everywhere, error visibility in
      status/dashboard. Full findings: KB_21.

## v3 — Real-time Discord, component architecture, live testing  ✅ DONE

- [x] **Discord's own rate-limit headers** (KB_23): RestBucketManager
      consumes X-RateLimit-* per route/bucket (per-guild via Discord's bucket
      ids); 429 Retry-After + Global cooldowns honored fleet-wide; sparse
      user-token headers handled honestly (researched vs docs + discord.py-self)
- [x] **Real-time cache-first operation** (KB_23): per-account DiscordCache
      ingested from READY (zero fetches); sends validate against cache;
      open_dm cache-first; fetches single-flight (one call, others wait)
- [x] **Flow aborts with reasons** (KB_23): TargetGone/AccessDenied stop
      THAT account's flow with a human reason; other accounts unaffected;
      soulcopy is the reference implementation (v1.2.0)
- [x] **Component architecture** (ARCHITECTURE.md): named managers
      (Account/Plugin/Profile/Task Scheduler/Rate Limit/Event Bus/Settings/
      Database/Logging) with stable facade imports + live/testing adapter
      diagram
- [x] **Task Scheduler** (KB_24): interval jobs (+jitter) persisted in DB,
      fired through the FULL safety stack, per-job containment, CLI +
      dashboard parity
- [x] **Profile Manager** (KB_24): conservative/balanced presets + custom
      profiles; apply sets rules/delays/lane per account (floor still guarded)
- [x] **Live testing suite** (tests_live/live_suite.py): opt-in real-Discord
      checks (gateway, cache-from-READY, REST intake, one-message round-trip)
      with disposable-account env gating and hard safety minimums
- [x] v2.3 — SendEngine: FarmLite's serialized per-account send lane
      (prefixrate spacing, pause-at-0, named coalescing, priority) — a
      single account can never have two requests in flight simultaneously;
      CLI/dashboard parity + persistence; 15 tests. Docs: KB_22.

## Backlog (nice-to-have, not requested)

- [ ] Browser frontend: keyboard shortcuts, mobile layout polish
- [ ] Audit page: CSV export button
- [ ] Reconnect policy editable from the dashboard (currently config/CLI only)
- [ ] Per-guild/per-channel scoped rate buckets (dimension beyond message/dm)
