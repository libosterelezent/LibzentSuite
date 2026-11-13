# LibzentSuite UI Remediation Plan

Prioritized from the audit reconciliation. Phase: investigation complete;
remediation pending operator approval. No production-code changes made
during the audit.

---

## Priority 1 — Immediate correctness risks

### P1.1 — Dialog focus trap (a11y)

**Finding:** E — `ui.js dialog()` has no Tab focus trap. A keyboard user can Tab past the dialog into the background page. The theme drawer (theme.js:900–923) has a working focus trap that can be copied.

**Files:** `interfaces/dashboard/static/js/ui.js` (dialog function, ~line 119)

**Fix:** Add a document-level keydown listener (active only while the dialog is open) that:
- Intercepts Tab and cycles focus within the dialog's focusable children
- Intercepts Escape and calls `close()`
- Removes the listener on close

**Acceptance:** With the ability dialog open, pressing Tab repeatedly cycles through only the dialog's focusable children. Escape closes the dialog. The opener regains focus.

**Risk:** Low — additive change, no existing behavior modified.

**Test:** Manual keyboard test + structural pin (the dialog function must contain a focus-trap comment or helper reference).

---

### P1.2 — Fleet Hub cancel button on terminal tasks

**Finding:** B — `renderQueue()` may show a cancel button for `completed` tasks.

**Files:** `interfaces/dashboard/static/js/views/groups.js` (renderQueue)

**What the code does:** the `terminal` variable correctly gates the cancel button (`completed || cancelled → no cancel`). **After re-audit: this is WORKING CORRECTLY.** The `terminal` check was verified against all 7 assignment states. No fix needed.

**Verdict:** FALSE POSITIVE — the original audit misattributed the issue.

---

### P1.3 — Test suite flake triage

**Finding:** Pre-existing timing flakes in `test_task_scheduler` (7 failures) and `test_dashboard_http.test_peer_close_before_response_no_traceback`.

**Files:** `tests/unit/test_task_scheduler.py`, `tests/unit/test_dashboard_http.py`

**What was done:** `test_task_scheduler` was de-flaked in `d32ee99` (wait_until predicates replace wall-clock settles). Two consecutive full runs green. The `test_peer_close_before_response_no_traceback` failure was verified as pre-existing on HEAD (fails with all changes stashed).

**Remaining:** The `test_dashboard_http` peer-close failure is NOT related to the dashboard UI work. It tests the HTTP server's handling of a client disconnect before the response is written. The root cause is in `server.py`'s connection handler, not in any UI code. This is tracked separately.

**Verdict:** RESOLVED for test_task_scheduler. test_dashboard_http peer-close is a separate issue.

---

## Priority 2 — Design consistency

### P2.1 — Settings tab pattern: apply to all schema-driven editors

**Finding:** F1 (fixed for the ability dialog) — the same detached-form pattern existed in `plugins.js` registry card settings editors. The shared `schemaField`/`collectSchema` helpers are already used, but the form-attachment and save-then-reload patterns should be verified there too.

**Files:** `interfaces/dashboard/static/js/views/plugins.js`

**Fix:** Verify that `plugins.js` schema editors follow the same attach → collect → save → reload pattern. If they already do, no change needed. If not, align them.

**Acceptance:** Open a plugin's settings editor from the registry, change a value, save, and verify the persisted value renders.

---

### P2.2 — Fleet Hub queue: action state gating

**Finding:** B — the queue renders actions conditionally, but the "resume" action for SUSPENDED tasks is labeled "resume" while the actual op is `resume_task` (requeues to QUEUED). The label suggests it resumes a paused task, but it actually moves it back to the assignment queue.

**Files:** `interfaces/dashboard/static/js/views/groups.js` (renderQueue)

**Fix:** Rename the button label from "resume" to "requeue" (or "return to queue") to match the actual operation. The backend op name `resume_task` is correct; only the UI label is misleading.

**Acceptance:** The button label matches the actual state transition (SUSPENDED → QUEUED).

---

### P2.3 — Staging board z-order and visibility

**Finding:** The `Main / Home (variants)` variant container (288×576) at (1527,-40) extends above the page top edge and below into the staging area. Its children (Default and Active variant boards) may visually overlap the spec rows after the reorganization.

**Files:** Penpot page structure (not repo code)

**Fix:** In Penpot, move the `Main / Home (variants)` variant container to a dedicated "Components" section at (1500, 900) or reposition the spec rows to y=700+ to avoid visual overlap. Verify in the Penpot UI.

**Acceptance:** No visual overlap between the variant container and the spec rows on the Penpot canvas.

---

## Priority 3 — Design consistency and accessibility

### P3.1 — Consolidate emptyState implementations

**Finding:** D — `ui.js emptyState()` and `render.js emptyState()` are separate functions with equivalent output.

**Files:** `ui.js` (emptyState), `render.js` (emptyState)

**Fix:** Have render.js use ui.js's `emptyState()` (both are global scripts; ui.js loads before render.js). Delete the render.js copy.

**Acceptance:** Builder-rendered pages show the same empty-state styling as view-rendered sections.

---

### P3.2 — Add Hover variants to NavItem component set

**Finding:** The Home variant set has Default + Active but not Hover. The other 7 NavItem components have only a Default state.

**Files:** Penpot page `LibzentSuite — Sidebar v1`

**Fix:** Add Hover variants to the Home container and create Hover variants for the other 7 components. The Hover state: `#1B1B1B` fill, icon `#A0A0A0`, label `#F2F2F2` weight 400.

**Acceptance:** All 8 components have Default + Hover variants; Home also has Active.

---

### P3.3 — Add keyboard-focus state to NavItem components

**Finding:** The Focus state (transparent fill, `#F2F2F2` 1px ring) is documented in the states spec but not implemented as a Penpot variant.

**Files:** Penpot page `LibzentSuite — Sidebar v1`

**Fix:** Add Focus variants (1px `#F2F2F2` border, no fill change) to demonstrate the keyboard focus contract.

**Acceptance:** Each NavItem component has Default + Focus variants; the spec strip shows all 4 states.

---

### P3.4 — Responsive breakpoints audit

**Finding:** E — the existing breakpoints (900px, 640px, 430px, 360px) collapse grids and hide the sidebar, but there is no documented tablet-specific layout (768–1024px range).

**Files:** `shell.css` (responsive rules), `components.css` (grid breakpoints)

**Fix:** Document the current responsive behavior. The 900px breakpoint collapses `.grid-2` to single-column, which is correct for tablet. The 640px breakpoint handles narrow mobile. No additional breakpoints are needed unless the operator specifies tablet-specific layouts.

**Acceptance:** Test at 768px and 1024px viewports; verify no horizontal overflow and readable layout.

---

## Priority 4 — Maintainability (deferred)

### P4.1 — Extract preset-operation helpers

**Finding:** A — preset bind/delete/load calls are duplicated across the Presets tab, the Profile gallery, and the MEMORY section.

**Files:** `accounts.js`

**Fix:** Extract shared preset-operation helpers (e.g. `presetBind(account, plugin, name)`, `presetDelete(plugin, name)`, `presetRefresh(ex, account)`) into a module-level scope. All three surfaces call the shared helpers.

**Defer rationale:** The API is the single source of truth — UI duplication is presentational, not behavioral. Extract when touching accounts.js for other reasons.

---

### P4.2 — Semantic status colors in the token system

**Finding:** The Penpot palette and the token JSON have no ok/warn/err colors. base.css defines `--ok`, `--warn`, `--err` but these are not in the Penpot library.

**Files:** `docs/design/tokens.json`, Penpot library

**Fix:** Add `color/status-ok`, `color/status-warn`, `color/status-err` to the token JSON and the Penpot library when semantic status colors are needed by plugins.

**Defer rationale:** The current monochrome design doesn't use semantic status colors in the sidebar or hub. Add when a view requires them.

---

## Implementation sequence

1. P1.1 (dialog focus trap) — single-file fix, immediate a11y improvement
2. P2.2 (rename "resume" label) — one-line fix, correctness of terminology
3. P3.1 (consolidate emptyState) — removes a duplicate function
4. P2.1 (verify plugins.js settings editors) — read + targeted fix if needed
5. P2.3 (Penpot variant container overlap) — Penpot UI fix, no code
6. P3.2+P3.3 (Penpot Hover/Focus variants) — Penpot UI additions
7. P3.4 (responsive audit) — documentation + testing
8. P1.3 (remaining scheduler flakes) — if any new flakes appear
9. P4.1–P4.2 (deferred maintainability items)

Dependencies: P1.1 is independent. P2.x items depend on the Fleet Hub being the sole groups surface (already done). P3.x items are Penpot-only. P4.x items are deferred until the code implementation adopts the design tokens.
