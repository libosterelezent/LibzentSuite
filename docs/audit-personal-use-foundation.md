# LibzentSuite — Personal-Use Production Foundation Audit

**Date:** 2026-10-26 · **Branch:** `feat/ui-rebuild` @ `4cb7873` (+ local `b1f6f85`, `8b11010`) · **Baseline suite:** 2,351 tests, green (Linux/3.13, `uv`-locked env identical to CI)

**Scope:** single-operator production readiness. Multi-user, Django, and framework-rewrite proposals are out of scope by charter (§13 of the task).

---

## 1. Architecture as it exists

### Composition and dependency direction

```
main.py (signals, surfaces, exit codes)
  └─ core/bootstrap.py::build()          ← THE composition root (async, awaited)
       ├─ Credentials / CredentialService ── state/credentials.key or LZ_MASTER_KEY
       ├─ config.load_config               ← config/*.json (boot-time inputs, loud failures)
       ├─ Database (aiosqlite, WAL, migrations 1–9, repositories)
       ├─ LogService(+DB sink) / AuditRecorder / EventBus
       ├─ RateLimitManager ← SyncGroupManager membership
       ├─ SettingsManager (defaults ← global file ← profile ← account)
       ├─ TaskScheduler ← FleetPolicy.dispatch_guard ← FleetService
       ├─ PluginManager (loader → per-(account,plugin) instances)
       ├─ AccountManager (lifecycle governor, sender hook, cache)
       ├─ PluginDataStore (state/plugin-data.db — separate WAL file)
       ├─ FleetReconciler (30 s idempotent passes)
       └─ ControlAPI (the one facade) → Framework → main wires surfaces
```

Dependency direction is clean: `interfaces/*` → `ControlAPI` → services → repositories/adapters. Transport modules do not import managers; plugins see only `PluginContext`/`PluginServices`. Composition happens exactly once, in `bootstrap._assemble` — no service-locator, no hidden imports at the boundaries (lazy imports inside `adapters/discord` and `main.py` are deliberate and documented).

### Request path (dashboard → operation)

`static/js` SPA → `fetch` REST (`X-Dashboard-Token`/`?token=`) → `interfaces/dashboard/server.py` handler thread (auth → strict-JSON validation → 1 MB cap → main-loop hop with 504 budget) → `ControlAPI` method → owning service → repository → JSON response. Live push is the reverse: service → `EventBus` → server broadcaster → per-client WS queues. The SPA store consumes typed WS frames; router mount-generation fencing prevents stale async clobbering (regression-tested).

### Automation task lifecycle

`PluginServices` request → `TaskScheduler.submit` (priority queue, one execution lane per account) → dispatch guard (FleetPolicy eligibility + revalidation) → `RateLimitManager.acquire` (floor 1.5 s, jitter, server-429 cooldowns, optional global lane) → final validation → sender hook → adapter → history + audit + events. Cancellation is cooperative with a bounded re-reap; timeouts are terminal (never auto-retried — anti-duplicate-send invariant, tested); retries only for scheduler-classified transient errors. Shutdown drains with a bounded grace, then bounded re-reap of survivors.

### State ownership (authoritative vs derived)

| State | Authoritative | Derived/cached |
|---|---|---|
| Accounts, settings layers, policies, sync groups, fleet projection, plugin profiles, history/audit | SQLite (`state/libzent.db`, migrations append-only) | in-memory runtime mirrors, reloaded on boot |
| Account tokens | Fernet ciphertext in DB; key in `state/credentials.key`/`LZ_MASTER_KEY` | decrypted cache for runtime use (documented honesty: process memory is out of scope) |
| Operational plugin data | `state/plugin-data.db` (scope lattice, optimistic versions) | — |
| Config files (`config/*.json`) | boot-time inputs, loud failure on malformed | DB/kv rows win after boot |
| Logs | DB with retention counts + stderr | Dashboard Logs page (client-filtered) |

### Plugin access surface

Plugins receive `PluginContext` (identity, settings live-view, scoped logger) and `PluginServices` (paced sends, DM/cache resolution, cooldowns, state kv, data_* documents, fleet task handler opt-in). Discovery via `PluginLoader` (manifest + slug/name validation), one instance per (account, plugin), hooks best-effort with per-hook error recording, UI contribution via the builder (`core/dashboard_ui`), commands via `CommandSpec` through the self-command layer behind `AccessManager` (fail-closed). No production path imports a plugin's internals; the simulator's mention bridge is retired behind an env gate.

### Where credentials and secrets are

Per `knowledgebase/SECURITY.md`, verified against code this audit: ciphertext-only DB rows; master key 0600 outside the DB (or `LZ_MASTER_KEY`); atomic `O_EXCL` key creation (`credential_service._create_key_file`); redacting `__repr__`s; status surfaces as words (`db`/`env`/`missing`); `CREDENTIAL_GATED_ROUTES` hard-gate; boot key-mismatch canary; boot permission audit (warn-only, POSIX).

---

## 2. Verified-strong areas (explicitly not to be rewritten)

| Area | Evidence |
|---|---|
| Facade + transport separation | `interfaces/dashboard/server.py` maps 1:1 onto `ControlAPI`; no manager imports in handlers; CLI calls facade only |
| Security posture | Loopback bind default; token auth on REST+WS; `Host`/`Origin` validation (DNS-rebinding/cross-site defense, `server.py` `_loopback` block); `log_message` redacts `?token=` → `?[redacted]` (`server.py:2271`); strict JSON booleans; NaN/inf rejection; body/frame caps; 504 hop budget |
| RFC 6455 subset (`ws.py`) | Mask enforcement, RSV checks, non-minimal length rejection, close-code validation, 1 MB frame cap, 30 s frame deadline — audited verbatim with one authorized deviation |
| Config loading (`core/config.py`) | Malformed JSON → `ValueError` naming path + detail → clean exit 2 at boot; BOM tolerated; `_comment` keys stripped; unknown-key and flat-mode typo warnings; shape validation |
| Persistence | SQLite WAL + append-only migrations 1–9; optimistic versioned writes for plugin data; retention counts on logs/audit; runtime state is DB-backed so no torn-file risk for runtime mutations |
| Scheduler / limiter / fleet | Documented invariants + 2,351 tests; dispatch-guard fail-open-with-log documented; timeout-is-terminal anti-duplicate invariant tested |
| `main.py` lifecycle | SIGTERM/SIGINT/SIGHUP handlers per surface; graceful `framework.shutdown()` in `finally`; clean exit-2 paths for boot/bind errors; no raw tracebacks to users |
| Event bus | Typed events, handler isolation, bounded drain with undelivered-event logging, cancellation attribution helpers |
| Tests + CI | 2,351 tests green on Linux and CI; ruff lint+format gates; mypy advisory ratchet; pinned action versions; `uv sync --locked` exact-lock installs |

---

## 3. Confirmed defects found — and their status

| # | Defect (evidence) | Status |
|---|---|---|
| D1 | `benchmark/__init__` ↔ `benchmark.cli` circular import (`SCHEMA` via package root during partial init) — masked all 12 benchmark tests on CI | **Fixed** `4cb7873` (import from defining module) |
| D2 | `Sampler.stop` suppression narrowed by the SIM105 sweep: `except (CancelledError, Exception)` → `suppress(Exception)`; `CancelledError` (BaseException) propagated out of `session.stop()` at teardown | **Fixed** `b1f6f85` (explicit tuple restored; ERROR_HANDLING addendum codifies the await-boundary rule) |
| D3 | `PluginDataStore` close only on the build-failure rollback path → leaked non-daemon aiosqlite thread → interpreter exit hang (CI run 110) | **Fixed** `eb3871d` (shutdown step list + regression test) |
| D4 | Scheduler test races (`settle(0.3)` vs timeout sweep; history-row trailing the status flip) | **Fixed** `8b11010` (wait_until predicates; 5/5 stable) |
| D5 | CI pyflakes gate blocked 20+ runs on one unused import | **Fixed** `7cea744` + superseded by ruff gates |
| D6 | Fleet-seam source-text pin broken by formatter wrapping | **Fixed** (whitespace-normalized assertion) |

**Open confirmed defects: none.** Every defect verified during this audit cycle is fixed, tested, and CI-green (`run 40188204333`, all steps success, 2,351 tests in 409 s).

---

## 4. Non-defects — suspicions the code disproved

Recorded so the next auditor does not re-chase them:

1. **"WS has no auth"** — false: `ws.py` is protocol-only by design; the 401 gate lives in the server's upgrade path (`?token=`/header), plus Host/Origin validation.
2. **"`?token=` leaks into logs"** — false: `log_message` redacts query strings.
3. **"Runtime config writes can tear files"** — false: runtime mutations persist to SQLite (WAL); `config/*.json` are boot-time inputs never written at runtime; the only runtime file writes are the atomic key file, chmod'd DB/sidecars, and 0600 bench artifacts.
4. **"`probe` fixture is mutated by the test suite"** — stat-cache phantom: filtered diff is empty (mode-only noise from the Windows→Linux copy; `core.fileMode=false` set).
5. **"Missing log rotation"** — by design: logs persist to DB with retention counts; stderr belongs to the service manager (journald). No file handler to rotate.

---

## 5. Test coverage and gaps

**Coverage:** 2,351 tests — unit (domain, incl. bootstrap regressions, scheduler/limiter/fleet invariants, plugin profiles/scopes, dashboard static pins, HTTP+WS acceptance), integration (real composition over the simulator), simulation scenarios, stress/soak. Dashboard acceptance pins user-observable behavior (payloads, persisted results, redaction, loading/error states, WS seam). CI: pyflakes-superseding ruff gates + advisory mypy + pinned actions + exact-lock installs.

**Gaps (honest):**
- `tests/` tree is outside ruff scope (4 pre-existing findings in `test_task_scheduler.py`) — ratchet later, do not auto-fix blind.
- mypy strict is advisory with a 1,101-error baseline (81 files) — ratchet module-by-module as touched.
- No automated startup/shutdown smoke test of the REAL `main.py` process tree (covered indirectly via `StderrClaimWiringTests` and integration teardown; a subprocess smoke test would close it).
- Benchmark tests still require POSIX (`import resource`); on Windows the subsystem is un-importable (documented; Linux is the dev/CI platform now).

---

## 6. Stage B plan (smallest changes that deliver real benefit)

1. **P1 — preserve and verify what this session fixed** (done: D1–D6 above, each with regression tests).
2. **P2 — `uv sync`/`uv run` documentation parity**: README already updated; knowledgebase CONFIGURATION/TESTING addenda for the uv flow (append-only) — keeps operator docs truthful.
3. **P3 — ratchet, on touch**: mypy strictness and tests-tree linting expand only when a module is modified anyway (per AGENTS.md §8; no blind sweeps).
4. **P4 — optional operator hardening (no code by default)**: document `DASHBOARD_TOKEN` as effectively-required (the no-auth mode already warns loudly), and `state/` backup cadence (the DB+key pairing note from the migration).

Nothing in P2–P4 changes behavior; P1 is complete. The foundation's remaining risk is concentrated in the mypy ratchet and the benchmark's POSIX-only import — both tracked, neither urgent.

---

## 7. Recommendation

Do **not** rewrite anything. The audit's central finding is that LibzentSuite's architecture already matches the personal-use production bar the task describes: one facade, one composition root, DB-authoritative state, loud config failures, a tested automation pipeline, hardened local transport, and a 2,351-test suite with CI gates. The session's confirmed defects are fixed with regression tests; the honest open items are ratchets and documentation parity. Future multi-user evolution should begin — when actually wanted — with a separate architecture review of the `ControlAPI` boundary and credential surface, exactly as §14 of the task anticipates.
