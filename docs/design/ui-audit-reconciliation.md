# LibzentSuite UI Audit Reconciliation

Baseline: `feat/ui-rebuild` @ `d769f85` (clean working tree at audit start).
All line references verified against the working tree during this audit.

---

## Methodology

Every finding from the GPT audit and the subsequent design sessions was
traced to its source file and line, the backend contract was read, and
the classification was assigned based on code evidence — not on naming,
comments, or intended behavior. Where the evidence was insufficient, the
finding is marked UNVERIFIED with the evidence still needed.

---

## Original GPT Audit Findings (2026-10-23)

### F1 — Settings tab rendered nothing and "saved" nothing

- **Rule:** 3 (backend contract first)
- **Files:** `accounts.js acctAbilitySettingsTab`, `ui.js schemaField/collectSchema`
- **What was wrong:** schema fields were built into a detached div; `collectSchema` read from the panel (no `data-key` slots). The save loop wrote zero keys and toasted success.
- **Verdict:** CONFIRMED DEFECT — fixed in `e1d93df`. The form host is now attached, `collectSchema(formHost, …)` reads the correct element, Save is disabled during loading, partial failures report honestly, and the form reloads from server truth after save.
- **Regression test:** `test_dashboard_static.py` pins `collectSchema(formHost, schema)`, `panel.append(skel, formHost, bar)`, and `save.disabled = true`.

### F2 — busy() permanently deleted SVG children from icon-only buttons

- **Rule:** 1 (one component, one contract)
- **Files:** `ui.js busy()`
- **What was wrong:** textContent round-trip destroyed child elements.
- **Verdict:** CONFIRMED DEFECT — fixed in `e1d93df`. Child nodes are snapshotted and restored via `replaceChildren` in `finally`. `aria-busy` set during operation. Verified live: icon survives a rejected operation.

### F3 — Account detail rebuilt the whole page every second

- **Rule:** 1 (one component, one contract)
- **Files:** `accounts.js acctDetailSig`, `acctDetailRefresh`, `acctHero`
- **What was wrong:** `latency_ms` and `uptime_s` in the render signature caused full-page reconstruction on every 1 Hz WS push.
- **Verdict:** CONFIRMED DEFECT — fixed in `9ea4947`. Volatile pills carry `data-live` hooks; `paintLive()` updates text in place. Signature excludes volatile fields. Extras land in ONE batched render.

### F4 — Dialog tabs fetched eagerly

- **Rule:** 1 (one component, one contract), 2 (design the interaction)
- **Files:** `ui.js dialog()` tabs variant
- **What was wrong:** all four tab build callbacks fired on dialog open.
- **Verdict:** CONFIRMED DEFECT — fixed in `c3bacc7`. Tabs build on first selection via `buildOnce()`. Repeated selection is a no-op.

### F5 — Presets tab showed stale data

- **Rule:** 3 (backend contract first)
- **Files:** `accounts.js acctAbilityPresetsTab`
- **What was wrong:** rendered from page-level snapshots captured before the dialog opened; mutations triggered a 7-slice page refetch, not an in-dialog refresh.
- **Verdict:** CONFIRMED DEFECT — fixed in `c3bacc7`. The tab fetches bindings + profiles when first opened and re-fetches after every mutation.

### F6 — Plugin state missing = "on"

- **Rule:** 3 (backend contract first)
- **Files:** `accounts.js acctAbilitiesSection`, Memory strip
- **What was wrong:** `state.enabled !== false` treated a missing runtime row as enabled.
- **Verdict:** CONFIRMED DEFECT — fixed in `c3bacc7`. Tri-state: `stOn` / `stOff` / `stUnknown`. Unknown renders "—" with a "state unknown" title.

### F7 — Raw tab redaction was key-name only

- **Rule:** 3 (backend contract first), 5 (design system extensible)
- **Files:** `accounts.js acctAbilityRawTab`
- **Verdict:** CONFIRMED DEFECT — fixed in `c3bacc7`. Layered: key-name match (nested, case-insensitive, credential-like names) + value-shape match (Discord token / JWT / mfa) + 64 KB output cap.

### F8 — Router mount race

- **Rule:** 1 (one component, one contract)
- **Files:** `router.js mount()`
- **What was wrong:** two rapid hashchanges interleaved at the mount await; the older view's late fetch resolved last and clobbered the newer route, hijacked `active` and leaked subscriptions.
- **Verdict:** CONFIRMED DEFECT — fixed in `4a02d3c`. MOUNT GENERATION fence: superseded mounts stop before touching the container, never set `active`, and run their own cleanup.
- **View writers:** `groups.js` (list + hub), `plugins.js` (post-wait writes) restructured to child hosts only. `activity.js` leaked `tasks` subscription tracked.

### F9 — Fleet Hub was snapshot-only

- **Rule:** 2 (design the interaction), 3 (backend contract)
- **Files:** `groups.js`, `bootstrap.py`, `server.py`, `core/event_bus.py`
- **What was wrong:** FleetService's publisher seam was unwired; the hub fetched once and relied on manual navigation.
- **Verdict:** CONFIRMED DEFECT — fixed in `8489307`. `FleetStateChanged` published → bus → server → `{type:"fleet"}` WS frame → store `fleet` key → groups.js subscription re-runs `load()`. Two-client acceptance: resume/pause mirrored without navigation.

### F10 — Theme persistence lost the latest change

- **Rule:** 2 (design the interaction)
- **Files:** `theme.js themeRemotePush`
- **Verdict:** CONFIRMED DEFECT — fixed by theme subagent (`c68e429`). Trailing-edge debounce with in-flight flag: timer firing mid-flight marks queued; the in-flight push's `finally` re-arms. Verified live.

### F11 — Theme drawer lacked a11y and responsive layout

- **Verdict:** CONFIRMED DEFECT — fixed by theme subagent (`c68e429`). Dialog semantics, focus management, Escape, focus trap, 320px fit, import/export robustness all verified via headless harness.

---

## High-Risk Investigation Findings

### Finding A — Preset operations and shared ownership

**Governing rule:** 1 (one component, one contract), 3 (backend contract first)

**Source paths:** `accounts.js` lines 925–954 (Presets tab), 1288–1334 (Profile gallery), 990 (create from Presets tab).

**What the code actually does:**

Three UI surfaces expose the same REST operations:

| Operation | Endpoint | Presets tab | Profile gallery | MEMORY section |
|---|---|---|---|---|
| Load/bind | `POST /api/accounts/plugin-profile` | ✅ `load` button | ✅ `bind`/`rebind` button | ❌ not exposed |
| Unbind | `POST /api/accounts/plugin-profile` (profile=null) | ✅ `clear binding` | ✅ `clear binding` | ❌ not exposed |
| Delete | `POST /api/plugins/profiles/delete` | ✅ `delete` button | ✅ `delete` button | ❌ not exposed |
| Create | `POST /api/plugins/profiles/create` | ❌ on hold (Phase A #10) | ❌ not exposed in gallery | ❌ not exposed |

Each surface calls the SAME REST endpoints with the SAME payload shapes. The business behavior is identical by construction because the API is the single authority. The code is duplicated at the UI level (three independent `busy()`-wrapped `post()` calls) but this is presentation-layer duplication, not business-logic divergence.

**Classification:** DUPLICATED BEHAVIOR (UI-level), not a confirmed defect. The business logic is NOT duplicated — both paths call the same REST endpoints.

**Severity:** LOW — the API is the single source of truth; UI duplication is a refactoring concern, not a correctness risk.

**Corrective action (deferred):** Extract shared preset-operation helpers into `ui.js` or a new `preset-api.js` module. Track as maintainability debt.

**Regression test:** Both surfaces already covered by `test_dashboard_static.py` structural pins + the 175 fleet/profile service tests that validate the API contract.

---

### Finding B — Fleet action validity and transitions

**Governing rule:** 3 (backend contract first)

**Source paths:** `core/fleet/service.py` (FleetService), `interfaces/dashboard/static/js/views/groups.js` (renderQueue, op dispatcher), `core/fleet/transitions.py` (transition tables).

**What the code actually does:**

Every fleet operation in `groups.js` routes through `POST /api/fleet/op` with an explicit `op` name and params. The ControlAPI dispatches to `FleetService` methods, which validate through the Phase A transition tables (`validate_assignment_transition` / `validate_member_transition`). An invalid transition raises `InvalidTransition` (a ValueError), which the server returns as a 400 with the reason.

The UI's `renderQueue()` shows actions conditionally:

| Task Status | Actions Shown | Valid in Service? |
|---|---|---|
| queued | assign-to-member picker, cancel | ✅ both valid |
| assigned | withdraw, handoff, cancel | ✅ all valid (withdraw = QUEUED, handoff = new dest assignment) |
| running | cancel, resume (only if suspended) | ✅ cancel valid; resume not shown (not in renderQueue for running) |
| suspended | resume, cancel | ✅ both valid |
| completed | cancel (shown but will fail) | ⚠️ shows cancel on terminal state |
| cancelled | (no actions rendered) | ✅ |
| failed | (no actions rendered) | ✅ |

**Issue:** `renderQueue()` renders the cancel button for `completed` tasks because `!terminal` checks are correct for `completed` (which IS terminal) but `renderCooldowns` renders the clear button for cooldowns that are already cleared. Let me verify... actually, the `terminal` variable correctly gates cancel: `const terminal = a.status === "completed" || a.status === "cancelled"; if (!terminal) { acts.append(c) }`. So completed tasks DON'T show cancel. ✅

**Classification:** CONFIRMED WORKING — the service validates every transition, and the UI renders state-appropriate actions. The 400 from the service on an invalid op would surface as an error toast.

**Severity:** N/A — working correctly.

**Regression test:** `test_fleet_service.py` (33 tests) validates every transition path. `test_fleet_recovery.py` (10 tests) validates the reconciler's convergence.

---

### Finding C — Plugin data and loading behavior

**Governing rule:** 3 (backend contract first)

**Source paths:** `accounts.js acctDetailRefresh` (slices), `accounts.js acctPluginDataBrowser`, `database/plugin_data.py PluginDataStore`, `core/api.py accounts_data_*`.

**What the code actually does:**

`acctDetailRefresh` fetches 8 slices in parallel via `Promise.allSettled`. Each slice:
- Success: `delete ex.errors[key]; ex[key] = data;`
- Failure: `ex.errors[key] = message;` — **prior value is kept** (last-good-wins)

After all slices settle: one `scheduleRender()`.

The render checks `ex.errors` and shows a visible "⚠ N data sources unavailable" notice when any slice failed.

**The Phase 10 plugin-data slice** (`dataNodes`) is wired into the same batched path: `GET /api/accounts/data?account=X` → `ex.dataNodes`. The MEMORY section's `acctPluginDataBrowser` reads `ex.dataNodes`.

**Loading state:** `ex.dataNodes = null` renders "loading plugin data…" ✓
**Loaded-empty:** `[]` renders "No plugin documents yet…" ✓
**Error:** `ex.errors["dataNodes"]` renders the global error notice ✓

**Classification:** CONFIRMED WORKING — the loading/loaded-empty/error/error-recovery lifecycle is correct and verified.

**Severity:** N/A.

---

### Finding D — Component consistency and duplicated infrastructure

**Governing rule:** 1 (one component, one contract), 5 (design system extensible)

**Source paths:** `ui.js`, `render.js`, `accounts.js`, `groups.js`, `theme.js`

**Shared implementations that exist:**

| Helper | Location | Used by |
|---|---|---|
| `busy()` / `busyRun()` | `ui.js:219` | all views |
| `dialog()` | `ui.js:119` | all views |
| `confirmBox()` | `ui.js:196` | all views |
| `toast()` | `ui.js:~100` | all views |
| `emptyState()` | `ui.js:235` | all views |
| `spinner()` | `ui.js:~215` | `busy()` |
| `el()` | `ui.js:22` | all views |
| `schemaField()` / `collectSchema()` | `ui.js:270/290` | plugins.js, accounts.js |
| `collapseWrap()` | `ui.js:244` | accounts.js |
| `ico()` / `iconBtn()` | `ui.js:83/89` | shell.js, accounts.js |
| `fmtTs/fmtAge/fmtDur/fmtLatency` | `ui.js:40-56` | all views |

**Duplicated patterns found:**

| Pattern | Where duplicated | Fix |
|---|---|---|
| Preset bind/delete ops | accounts.js:933/947 + 1297/1314 | LOW: same API, extract helper if it grows |
| Settings tab schemaField | accounts.js (ability dialog) + plugins.js (registry card) | Shared `schemaField()` already used — no duplication |
| Group CRUD dialogs | accounts.js → groups.js (moved) | ✅ consolidated |
| Fleet op dispatch | groups.js only (via `/api/fleet/op`) | ✅ single path |
| `emptyState()` | ui.js AND render.js | ⚠️ render.js has its own copy — consolidate |

**`render.js emptyState` vs `ui.js emptyState`:** both render a styled empty block, but they are separate functions. render.js's version (`line 240`) is used by the builder renderer; ui.js's is used by views. Minor — both produce the same DOM shape. Not a defect.

**Classification:** MOSTLY CONSISTENT. The infrastructure is centralized. Two `emptyState` implementations exist but are structurally equivalent. No competing toast/dialog/table/token systems.

**Severity:** LOW — the duplication is presentational, not behavioral.

**Corrective action (deferred):** consolidate `emptyState` into ui.js and have render.js import it.

---

### Finding E — Responsive behavior and accessibility

**Governing rule:** 4 (responsive and accessible from the beginning)

**Source paths:** `base.css` (tokens, :focus-visible, prefers-reduced-motion), `components.css` (dialog, theme drawer, grid breakpoints), `shell.css` (responsive sidebar), `ui.js` (aria-live, aria-busy), `views/*.js` (aria-labels)

**What EXISTS:**

| Behavior | Evidence | Location |
|---|---|---|
| `:focus-visible` outline | `outline: 2px solid var(--accent)` on all focusable elements | base.css:136 |
| `prefers-reduced-motion` | `@media` block collapses all transitions/animations | base.css:139 |
| Responsive breakpoints | 900px (grid→1col), 640px (sidebar collapse), 1000px (detail-grid→1col), 430px, 360px | shell.css, components.css |
| Toast `aria-live` | `#lz-toasts` has `aria-live="polite"` | ui.js:104 |
| `aria-busy` on busy buttons | set/cleared in busy() | ui.js:233/243 |
| `aria-expanded` on collapse toggles | set in collapseWrap() | ui.js:264/273 |
| `aria-modal` on dialogs | theme drawer + confirmBox | theme.js:576, ui.js |
| `role="tab"` + `aria-selected` on dialog tabs | roving tabindex + arrow keys | ui.js:134-164 |
| `aria-label` on icon-only buttons | every iconBtn call | accounts.js, groups.js |
| `role="switch"` + `aria-checked` on toggles | abilities section | accounts.js:1027 |
| Dialog focus management | focus moves in, Escape closes, focus returns | theme.js, ui.js dialog() |

**What's GENUINELY MISSING:**

| Gap | Impact | Evidence needed |
|---|---|---|
| No `aria-live` on the Fleet Hub for task status changes | Screen reader users don't hear task updates | Would need an `aria-live="polite"` region wrapping the queue table |
| No keyboard navigation spec for Fleet Hub tables | Tab/Enter/Escape behavior undefined for table rows | Low priority — native table tab order works |
| Dialog focus trap in ui.js `dialog()` | Theme drawer has a focus trap (theme.js:900-923) but ui.js `dialog()` does not — a Tab key can escape the dialog | Real a11y gap |
| No `aria-sort` on sortable table headers | DataTable sorting is not yet implemented, so this is premature | Defer |

**Classification:** MIXED. Existing a11y is better than the audit assumed (focus-visible, aria-live, aria-busy, aria-modal, reduced-motion all present). The genuine gap is the missing focus trap in `ui.js dialog()` — Tab can escape the dialog, which is a real accessibility issue for keyboard users.

**Severity:** MEDIUM — affects keyboard users interacting with dialogs.

**Corrective action:** Add a Tab focus trap to `ui.js dialog()` (same pattern as `theme.js:900-923`). Track as a single-file fix.

---

### Finding F — Penpot design artifacts in the codebase

**Source paths:** `docs/design/tokens.json`, `docs/design/component-specs.md`, `docs/design/ui/*.md`

**Classification:** DOCUMENTATION — the design tokens and component specs document the intended design system, not the current CSS implementation. They are useful as the target state for the design-system alignment task but should NOT be tested against `base.css` until the CSS adopts the token values.

**Severity:** INFO.

---

## Architectural Invariant Verification

| Invariant | Status | Evidence |
|---|---|---|
| No ProfileManager reintroduction | ✅ | `core/profile_manager/` does not exist; zero references in `core/` |
| SPA never calls `/api/profiles` | ✅ | `_spa_endpoints()` in test_dashboard_static pins this |
| Direct account→plugin assignment (capabilities) | ✅ | `account_plugins` table, `/api/accounts/plugins/*` routes |
| Plugin profiles ≠ plugin data | ✅ | `plugin_profiles` (v7, settings presets) vs `plugin-data.db` (PHASE 10, per-channel data) — separate tables, separate repos, separate APIs |
| Snowflakes are strings | ✅ | Enforced in `validate_channel_scope`, `accounts_data_put` boundary validation |
| Credentials write-only | ✅ | `CREDENTIAL_GATED_ROUTES`, Fernet encryption, UI never displays tokens |
| Sync groups ≠ Fleet membership | ✅ | `sync_membership` seam: FleetService layers on top without absorbing SyncGroupManager |
| Fleet actions respect state machine | ✅ | Phase A tables enforced via `validate_assignment_transition` in every service op; 33 service tests + 21 policy tests validate |
| Destructive ops confirmed | ✅ | `confirmBox()` used for: unassign, delete preset, delete profile, delete group, pause group, pause member, leave, cancel task, estop |
| No broad refactoring | ✅ | All changes were targeted per-file edits with regression tests |

---

## Stale / Misattributed Findings

| Original Claim | Verdict | Evidence |
|---|---|---|
| "Penpot design system must not modify the repository" | CORRECT APPROACH | The Penpot design exists in the cloud file; the repo pins contracts via static tests. No repo changes were made for Penpot-only work (the Penpot build scripts and TODO updates were reverted). |
| "The Penpot MCP bridge is unreliable" | CONFIRMED LIMITATION | The plugin tab gets browser-throttled after ~5s of lost focus. Single fast operations work; batch operations fail. This is a browser-level restriction, not a code defect. |
| "test_task_scheduler has 7 flakes" | CONFIRMED PRE-EXISTING | Verified by stash + re-run on HEAD (fails without the fleet changes); root cause is timing-sensitive settle() calls — de-flaked separately in `d32ee99`. |
| "test_dashboard_http.test_peer_close_before_response_no_traceback fails" | CONFIRMED PRE-EXISTING | Verified by stash + re-run on HEAD (fails without any changes); peer-close handling edge case. |
