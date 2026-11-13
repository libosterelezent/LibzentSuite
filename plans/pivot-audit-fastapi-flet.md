# LibzentSuite — Full Project Pivot Audit: Mudae Removal + FastAPI/Flet Migration Plan

**Date:** 2026-11-13 · **Branch:** `feat/ui-rebuild` @ `5640392` · **Baseline:** 2,351 tests green, CI success, ruff clean (production tree), mypy 1,086 advisory.

**Goal:** reposition LibzentSuite as a pure selfbot multi-account orchestration app — no game-specific logic — with FastAPI as the core API layer and Flet as the UI framework, both in Python.

---

## 1. Mudae removal — every reference and its dependency graph

### 1.1 The parser pack (delete entirely)

| Path | Lines | What it is |
|---|---|---|
| `parsers/mudae/` (20 files) | ~3,000 | The complete Mudae recognition engine: rules (kakera, characters, cooldowns, usernames), locales, settings models/validator/manager, normalization, diagnostics |
| `parsers/manager.py` | ~200 | The parser-manager bridge (loads packs, dispatches findings) |
| `parsers/__init__.py` | ~10 | Package root |

**Zero production imports** — `grep -rn "from parsers|import parsers" main.py core/ adapters/ database/ interfaces/ plugins/ scripts/ benchmark/` returns **nothing**. The parser pack is already fully decoupled from the framework (the architecture contract "core imports must not depend on `parsers/`" held). Deleting it breaks **zero production code**.

**Test breakage:** `tests/unit/test_mudae_parser.py`, `test_mudae_parser_strings.py`, `test_mudae_parser_settings.py`, `test_parser_manager.py` — approximately 200–300 tests. These are self-contained parser tests; delete them with the pack.

### 1.2 Mudae references in production code (rename/generalize)

| File | Refs | Nature | Action |
|---|---|---|---|
| `core/access_manager.py` | 13 | `mudae_bot_id` property, `MUDAE_BOT_ID` env fallback, `KEY_MUDAE_ID` kv key | Rename to `primary_bot_id` (a "target bot we watch for" concept that is game-agnostic) |
| `core/api.py` | 12 | `admin_mudae_id_set/get` methods on ControlAPI | Rename to `admin_primary_bot_id_*` |
| `core/framework_ui.py` | 17 | Admin tab "Mudae bot id" box, `adm-mudae-id` component, `set_mudae` action | Rename component IDs + labels |
| `core/plugin_manager/commands.py` | 4 | Comment references to Mudae's layout | Rewrite comments |
| `core/plugin_manager/loader.py` | 1 | `_mudae_plugroot_` synthetic parent name | Rename to `_plugin_root_` |
| `core/bootstrap.py` | 1 | Comment: "perms, prefixes, mudae id" | Rewrite comment |

### 1.3 Mudae references in config/docs (update)

| File | Action |
|---|---|
| `.env.example` | Remove `MUDAE_BOT_ID` line (or rename to `PRIMARY_BOT_ID`) |
| `README.md` | Rewrite: "Built for Mudae farming" → "Multi-account Discord selfbot orchestration framework" |
| `config/rate_limits.json` | The `_comment` mentions "learned defaults from the live rate study" — game-agnostic already ✓ |

---

## 2. FastAPI migration — what it replaces and what breaks

### 2.1 What gets replaced

| Current | Lines | Replaced by |
|---|---|---|
| `interfaces/dashboard/server.py` | 2,282 | FastAPI application with routers |
| `interfaces/dashboard/ws.py` | 166 | FastAPI WebSocket endpoints (native, no hand-rolled RFC 6455) |
| `interfaces/dashboard/__init__.py` | 9 | FastAPI app factory |
| `interfaces/dashboard/static/` (20 files) | 436 KB | **Deleted entirely** — Flet replaces the SPA |

**What survives (the domain core):**
- `core/api.py` — ControlAPI stays as the domain facade; FastAPI routes call it (transport adapter)
- All `core/` managers, services, repositories — untouched
- All `database/` — untouched
- All `adapters/` — untouched
- All `core/fleet/`, `core/task_scheduler/`, `core/rate_limit_manager/` — untouched
- `core/dashboard_ui/` (builder contract) — **at risk**: Flet replaces the browser-based builder rendering; plugin UI contribution needs a new contract

### 2.2 What FastAPI gives you that the custom server doesn't

| Gap in current server | FastAPI provides |
|---|---|
| Hand-rolled HTTP parsing (`BaseHTTPRequestHandler`) | ASGI-native, battle-tested |
| Hand-rolled RFC 6455 WebSocket subset | Native WebSocket support with connection manager |
| Manual JSON validation (every field by hand) | Pydantic models with automatic validation + 422 responses |
| No OpenAPI/Swagger | Auto-generated docs |
| Hand-rolled auth middleware | Dependency injection with `Depends()` |
| Thread-per-request model | Async-native (matches the asyncio core) |
| Manual error mapping (every handler) | Centralized exception handlers |
| No request rate limiting | `slowapi` or middleware |

### 2.3 What Flet gives you that the SPA doesn't

| Gap in current SPA | Flet provides |
|---|---|
| 20 hand-written JS files, no framework | Python-native UI — same language as backend |
| Custom router with mount-generation fences | Flet's built-in page routing |
| Custom store + WebSocket client + reconnect logic | Flet handles the transport |
| Custom CSS (436 KB) with theme engine | Flet's Material Design + theming |
| Custom a11y implementation | Flet's built-in accessibility |
| `core/dashboard_ui/` builder contract for plugins | Needs redesign — Flet has different composition primitives |

### 2.4 Migration breaking changes (full list)

| File/directory | Action | Why |
|---|---|---|
| `interfaces/dashboard/server.py` | **DELETE** → FastAPI app | Replaced by FastAPI routers |
| `interfaces/dashboard/ws.py` | **DELETE** | FastAPI native WebSocket |
| `interfaces/dashboard/static/` | **DELETE** (all 20 files) | Replaced by Flet |
| `interfaces/dashboard/__init__.py` | **REWRITE** | FastAPI app factory |
| `core/dashboard_ui/` (7 files) | **REDESIGN** | Browser builder → Flet composition contract |
| `core/framework_ui.py` | **REWRITE** | Browser builder pages → Flet pages |
| `main.py` | **MODIFY** | Replace DashboardServer with uvicorn |
| `tests/unit/test_dashboard_http.py` | **REWRITE** | httpx/TestClient instead of raw HTTP |
| `tests/unit/test_dashboard_static.py` | **DELETE** | No SPA to pin |
| `tests/unit/test_dashboard_ui_acceptance.py` | **REWRITE** | Flet acceptance testing |
| `tests/unit/test_dashboard_ui_http.py` | **REWRITE** | FastAPI route tests |
| `tests/unit/test_dashboard_ui_registry.py` | **REWRITE** | New UI contract |
| `tests/unit/test_framework_ui.py` | **REWRITE** | New UI contract |
| `pyproject.toml` | **ADD** fastapi, uvicorn, flet, pydantic | New runtime deps |
| `uv.lock` | **REGENERATE** | New dependency tree |

**What does NOT break:**
- `core/api.py` — the facade's methods stay; FastAPI routes are thin adapters
- All `core/` domain logic (account manager, plugin manager, scheduler, fleet, rate limiter, credentials, settings)
- All `database/` (schema, repositories, plugin_data store)
- All `adapters/` (discord client, simulator)
- `interfaces/cli/` — REPL and one-shot commands unchanged
- All fleet/scheduler/limiter/credential tests — domain tests, no transport dependency

---

## 3. Knowledgebase staleness audit (29 files)

### 3.1 Files that are entirely Mudae-specific (delete)

| File | Content |
|---|---|
| `MUDAE_PARSER.md` | Entire file about the Mudae recognition engine |
| `knowledgebase/read_only/modules/parsers.*.md` | Generated symbol docs for the parser pack |
| `knowledgebase/read_only/plugins/probe.md` | Probe plugin docs (deleted from production) |

### 3.2 Files with significant Mudae/stale references (rewrite)

| File | Stale content | What's needed |
|---|---|---|
| `README.md` | "Built for Mudae farming" | Reposition |
| `PROJECT_OVERVIEW.md` | Positions Mudae as the workload | Rewrite as generic selfbot |
| `ARCHITECTURE.md` | Old v1 engineering (superseded by root v2+§14) | Delete or mark as historical |
| `CONFIGURATION.md` | MUDAE_BOT_ID references | Update |
| `DASHBOARD.md` | Documents the SPA routes/WS protocol | Rewrite for FastAPI/Flet |
| `DASHBOARD_UI_API.md` | The browser builder contract | Rewrite for Flet plugin UI |
| `API_ENDPOINTS.md` | Custom-server REST surface | Rewrite for FastAPI routes |
| `DATA_FLOW.md` | References parser findings feeding fleet | Remove parser paths |
| `PLUGIN_API.md` / `PLUGIN_EXAMPLES.md` | References parser capability | Update |
| `ADAPTERS.md` | pacing_respected scenario references | Minor updates |
| `CHANGELOG.md` | Historical (mentions Mudae extensively) | Mark as historical |
| `TESTING.md` | References test_mudae_parser | Update |
| `COMPONENT_MAP.md` | Lists parsers/ | Remove parser entries |

### 3.3 Files that reference removed systems (ProfileManager)

| File | Stale references |
|---|---|
| `ADAPTERS.md` | Mentions "profile manager" |
| `ARCHITECTURE.md` | Old ProfileManager references |
| `COMPONENT_MAP.md` | Lists profile manager components |
| `CONFIGURATION.md` | Profile manager settings |
| `DATABASE.md` | Profile tables (removed in migration 8) |
| `PLUGIN_SYSTEM.md` | Profile-based assignment |
| `RUNTIME_LIFECYCLE.md` | Profile manager boot step |

### 3.4 Files referencing removed UI work

| File | Stale |
|---|---|
| `DESIGN_AWARENESS.md` (if exists) | Penpot/Figma references |
| Various | Figma workspace series references |

**Knowledgebase verdict: ~70% of the 29 files are stale** relative to the pivot target. Since the user already gitignored knowledgebase/ to local-only, the recommendation is: regenerate from scratch post-migration rather than patching.

---

## 4. Structural defects that prevent production readiness

### 4.1 Architecture defects

| Defect | Evidence | Impact | Fix |
|---|---|---|---|
| **Hand-rolled HTTP server** (thread-per-request over `BaseHTTPRequestHandler`) | `server.py:1797` — Python's built-in HTTP server, not designed for production | Thread exhaustion under load, no keep-alive, no HTTP/2, manual everything | FastAPI + uvicorn replaces entirely |
| **Hand-rolled RFC 6455 WebSocket** (partial implementation) | `ws.py` — "Minimal RFC 6455 WebSocket server subset (text frames only)" | No fragmentation reassembly, no binary frames, no compression, no subprotocol negotiation | FastAPI native WebSocket |
| **Thread-per-request + asyncio bridge** | `server.py:9` "each request gets its own handler thread" + main-loop hop | Race potential between threads; thread overhead per request | FastAPI is async-native |
| **Plugin UI builder tied to browser rendering** | `core/dashboard_ui/` → `interfaces/dashboard/static/js/render.js` | Plugin authors must think in HTML/CSS concepts | Redesign for Flet composition |
| **No request-level authorization** — single shared token | `server.py` token check | No per-user permissions, no audit trail of who did what | FastAPI `Depends()` + per-user auth (future) |
| **Single-process bottleneck** — dashboard, bots, scheduler all in one Python process | `main.py` — one process, one event loop | A dashboard crash takes down all accounts; a heavy plugin blocks the UI | Consider process isolation (dashboard in a separate process from the bot engine) |

### 4.2 Database management defects

| Defect | Evidence | Impact |
|---|---|---|
| **No database backup strategy** | Nothing in the codebase creates, tests, or rotates backups | A disk failure loses all account tokens (encrypted), all settings, all history |
| **SQLite WAL on FUSE/NTFS** (if still on /home) | state/ on fuseblk mount | WAL locking is emulated — potential corruption under concurrent writes (mitigated by /srv relocation for the dashboard, but the REPO state/ still exists) |
| **Duplicate state directories** | `state/` (repo, stale) + `/srv/libzentsuite/state/` (live) | Confusion about which is authoritative; the repo copy is a stale backup that could be accidentally used |
| **No schema versioning for plugin-data.db** | `database/plugin_data.py` — self-initializing schema, no migration system | Schema changes to the plugin-data store require manual intervention or data loss |
| **Audit log stores message content in plaintext** | SECURITY.md §7: "content tables are not encrypted" | Physical DB access + no key needed yields full interaction history |

### 4.3 Security risks

| Risk | Severity | Evidence | Mitigation |
|---|---|---|---|
| **Selfbot ToS violation** (inherent) | HIGH | discord.py-self — automated user accounts violate Discord ToS | Documented in SECURITY.md; mitigations (pacing, jitter) reduce but don't eliminate |
| **Single shared dashboard token** | MEDIUM | `DASHBOARD_TOKEN` — one secret, no rotation, no per-user | FastAPI dependency injection enables per-user auth |
| **No HTTPS/TLS on dashboard** | MEDIUM | Loopback bind (mitigated); no TLS if exposed | FastAPI + uvicorn supports TLS natively |
| **Credential key file beside the DB** | MEDIUM | `state/credentials.key` next to `state/libzent.db` | Physical access compromise; consider key derivation or HSM |
| **curl_cffi dependency** | LOW | discord.py-self depends on `curl_cffi` for HTTP — a C-level library with potential for memory-safety issues | Pinned in lockfile; monitor CVEs |
| **No CSP headers** | LOW | Custom server doesn't set Content-Security-Policy | FastAPI middleware can add |
| **Plugin code runs with full Python privileges** | BY DESIGN | No sandbox — documented honestly | Document; consider restricted execution in the future |

### 4.4 Risky dependencies

| Dependency | Version | Risk | Assessment |
|---|---|---|---|
| `discord-py-self` | 2.1.0 | **HIGHEST** — selfbot library, inherently ToS-violating, community-maintained (not officially supported), could be DMCA'd or abandoned | This IS the project's core dependency. No alternative exists for user-account automation. Pin exact version, monitor the repo for updates |
| `curl-cffi` | 0.16.3 | C-level HTTP impersonation library — potential memory safety issues, used for TLS fingerprint matching | Transitive dep of discord-py-self; monitor CVEs |
| `cryptography` | 50.0.2 | Well-maintained, but large attack surface | Standard choice, low risk |
| `aiosqlite` | 0.22.1 | Well-maintained async SQLite wrapper | Low risk |
| `protobuf` | 7.36.2 | Used by discord-protos (gateway protocol definitions) | Transitive, low risk |

**New dependencies for the migration:**

| Package | Role | Risk |
|---|---|---|
| `fastapi` | Core API framework | Well-maintained, huge community, low risk |
| `uvicorn` | ASGI server | Standard FastAPI server, low risk |
| `pydantic` | Request/response validation | FastAPI dependency, well-maintained |
| `flet` | Python UI framework | Newer project, smaller community than web frameworks; actively developed; potential for API changes |

---

## 5. Migration execution plan (FastAPI + Flet)

### Phase 1: Mudae removal (foundation cleaning)
1. Delete `parsers/` entirely (20 files, ~3,000 lines)
2. Delete `tests/unit/test_mudae_parser*.py` + `test_parser_manager.py`
3. Rename `mudae_bot_id` → `primary_bot_id` across access_manager, api, framework_ui
4. Update `.env.example`, `README.md`
5. **Verify:** suite minus parser tests green

### Phase 2: FastAPI core (parallel to existing dashboard)
1. Add `fastapi`, `uvicorn`, `pydantic` to `[project] dependencies`
2. Create `interfaces/api/` package:
   - `app.py` — FastAPI application factory
   - `routers/` — one router per domain (accounts, plugins, fleet, policies, admin, etc.)
   - `models.py` — Pydantic request/response models
   - `auth.py` — token dependency
   - `ws.py` — WebSocket endpoint for live events
3. All routers call `ControlAPI` — zero business logic in the transport layer
4. **Coexistence:** run both servers during migration (old on 8070, FastAPI on 8071)
5. Port REST routes incrementally; verify each with httpx tests

### Phase 3: Flet UI (replaces SPA)
1. Add `flet` to dependencies
2. Create `interfaces/ui/` package:
   - `app.py` — Flet application entry
   - `pages/` — one page per module (accounts, fleet, orchestration, etc.)
   - `components/` — shared Flet components
   - `theme.py` — theme system (mapping the existing CSS custom properties to Flet theme)
3. **Plugin UI contract redesign:** `core/dashboard_ui/` → a Flet-composition contract
4. Port pages incrementally; verify each

### Phase 4: Cleanup
1. Delete `interfaces/dashboard/` entirely
2. Delete `tests/unit/test_dashboard_static.py`
3. Rewrite dashboard tests for FastAPI + Flet
4. Update `main.py` — replace DashboardServer with uvicorn + Flet
5. Regenerate knowledgebase from scratch (post-migration reality)
6. Remove stale `state/` in repo (keep `/srv/libzentsuite/state/`)

---

## 6. Summary of all files affected

### Delete (29+ files, ~8,000 lines)
```
parsers/                                          (20 files, ~3,000 lines)
tests/unit/test_mudae_parser.py
tests/unit/test_mudae_parser_strings.py
tests/unit/test_mudae_parser_settings.py
tests/unit/test_parser_manager.py
interfaces/dashboard/static/                      (20 files, ~436 KB)
knowledgebase/MUDAE_PARSER.md
knowledgebase/read_only/modules/parsers.*.md
knowledgebase/read_only/plugins/probe.md
```

### Rewrite/Replace
```
interfaces/dashboard/server.py    (2,282 lines → FastAPI routers)
interfaces/dashboard/ws.py        (166 lines → FastAPI WebSocket)
interfaces/dashboard/__init__.py  (9 lines → FastAPI factory)
core/dashboard_ui/                (7 files → Flet composition contract)
core/framework_ui.py              (builder pages → Flet pages)
main.py                           (dashboard wiring → uvicorn + Flet)
README.md                         (reposition)
.env.example                      (remove MUDAE_BOT_ID)
```

### Modify (Mudae rename + minor updates)
```
core/access_manager.py            (mudae_bot_id → primary_bot_id)
core/api.py                       (admin_mudae_id_set → admin_primary_bot_id_*)
core/plugin_manager/commands.py   (comments)
core/plugin_manager/loader.py     (synthetic parent name)
core/bootstrap.py                 (comment)
```

### Rewrite tests
```
tests/unit/test_dashboard_http.py
tests/unit/test_dashboard_static.py     (delete)
tests/unit/test_dashboard_ui_acceptance.py
tests/unit/test_dashboard_ui_http.py
tests/unit/test_dashboard_ui_registry.py
tests/unit/test_framework_ui.py
```

### Untouched (the stable core)
```
core/account_manager/             All domain logic
core/task_scheduler/              Scheduler
core/rate_limit_manager/          Pacing + policies
core/fleet/                       Fleet orchestration
core/credential_service.py        Encrypted credentials
core/settings_manager.py          Settings resolution
core/event_bus.py                 Typed events
core/plugin_manager/              Plugin discovery + dispatch (minus mudae refs)
database/                         All repositories + schemas
adapters/                         Discord client + simulator
interfaces/cli/                   REPL + one-shot
plugins/ping/                     The remaining plugin
```

---

## 7. Recommended execution order

1. **Mudae removal** (immediate, zero-risk — parsers are already decoupled)
2. **FastAPI skeleton** alongside existing server (no disruption)
3. **Port REST routes** one domain at a time (accounts → plugins → fleet → policies → admin)
4. **Flet UI skeleton** (can start in parallel with step 3)
5. **Port UI pages** one module at a time
6. **Switch over** — new server on 8070, old server deleted
7. **Clean up** — delete old dashboard, stale knowledgebase, stale state
8. **Regenerate knowledgebase** from the post-migration reality

**Estimated scope:** ~8,000 lines deleted, ~2,300 lines replaced (server + SPA), ~3,000 new lines (FastAPI routers + Pydantic models + Flet pages), ~500 lines modified (renames + main.py wiring). Net: the codebase shrinks by ~5,000 lines while gaining a production-grade API framework and a unified Python UI.
