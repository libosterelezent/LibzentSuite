"""Dashboard UI error hierarchy.

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
"""
from __future__ import annotations

__all__ = [
    "UIError",
    "InvalidComponentIdError",
    "DuplicateComponentIdError",
    "InvalidChildError",
    "TreeValidationError",
    "FrozenLayoutError",
    "ActionRegistrationError",
]


class UIError(ValueError):
    """Base class for every dashboard UI error."""


class InvalidComponentIdError(UIError):
    """An explicit component id is not a valid 1-64 char [A-Za-z0-9_-] string."""


class DuplicateComponentIdError(UIError):
    """A component id is already used elsewhere in the same tree.

    Carries the colliding id plus the type and tree path of the
    existing component and of the conflicting newcomer, so callers can
    report exactly where the collision is.
    """

    def __init__(
        self,
        component_id: str,
        existing_type: str,
        existing_path: str,
        conflicting_type: str,
        conflicting_path: str,
    ) -> None:
        super().__init__(
            f"duplicate component id '{component_id}': already used by "
            f"{existing_type} at '{existing_path}', conflicts with "
            f"{conflicting_type} at '{conflicting_path}'"
        )
        self.component_id = component_id
        self.existing_type = existing_type
        self.existing_path = existing_path
        self.conflicting_type = conflicting_type
        self.conflicting_path = conflicting_path


class InvalidChildError(UIError):
    """A parent refuses this parent/child relationship (wrong type for the
    container, child already owned by another parent, unknown id, or the
    wrong category for a typed remove)."""


class TreeValidationError(UIError):
    """Structural tree problems: cycles, orphans, components with multiple
    parents, cross-branch duplicate ids, or interactive components whose
    action is not callable."""


class FrozenLayoutError(UIError):
    """A mutation was attempted on a component attached to a frozen layout."""


class ActionRegistrationError(UIError):
    """A non-callable was registered where an action callable is required."""
