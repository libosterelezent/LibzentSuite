# Module `core.dashboard_ui.errors`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/dashboard_ui/errors.py`

```
Dashboard UI error hierarchy.

Every dashboard-ui failure is a UIError (a ValueError, so both
interfaces can render it as a plain validation message). The
subclasses pinpoint what went wrong:

  * InvalidComponentIdError    — an explicit id fails the format rules;
  * DuplicateComponentIdError  — an id collides inside a tree (carries
                                 the existing and the conflicting
                                 component's type and path);
  * InvalidChildError          — a parent/child relationship the parent
                                 refuses (wrong type, already owned,
                                 unknown id, wrong category);
  * TreeValidationError        — structural problems: cycles, orphans,
                                 shared/multiple parents, cross-page
                                 duplicate ids, broken actions;
  * FrozenLayoutError          — mutation attempted after the layout was
                                 attached/frozen;
  * ActionRegistrationError    — a non-callable registered as an action.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `UIError` | exception | yes | Base class for every dashboard UI error. |
| `InvalidComponentIdError` | exception | yes | An explicit component id is not a valid 1-64 char [A-Za-z0-9_-] string. |
| `DuplicateComponentIdError` | exception | yes | A component id is already used elsewhere in the same tree. |
| `InvalidChildError` | exception | yes | A parent refuses this parent/child relationship (wrong type for the |
| `TreeValidationError` | exception | yes | Structural tree problems: cycles, orphans, components with multiple |
| `FrozenLayoutError` | exception | yes | A mutation was attempted on a component attached to a frozen layout. |
| `ActionRegistrationError` | exception | yes | A non-callable was registered where an action callable is required. |

### `DuplicateComponentIdError`

