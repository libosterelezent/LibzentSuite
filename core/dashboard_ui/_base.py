"""Dashboard UI builder primitives.

Argument-check helpers plus the UIComponent / UIContainer contract and
the interactive/input mixins every concrete builder derives from.
Package-internal: import the public classes from core.dashboard_ui (or
core.dashboard_ui.builders), not from here.
"""

from __future__ import annotations

import re
import uuid

from .errors import (
    ActionRegistrationError,
    DuplicateComponentIdError,
    FrozenLayoutError,
    InvalidChildError,
    InvalidComponentIdError,
    TreeValidationError,
    UIError,
)

__all__ = ["UIComponent", "UIContainer"]


_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_ID_LENGTH = 64


# ---------------------------------------------------------- argument checking
_CSS_SIZE_RE = __import__("re").compile(r"^[0-9]*\.?[0-9]+(px|%|em|rem|vh|vw|ch|ex|fr| vmin|vmax)$")


def _validate_css_size(value, prop: str) -> str:
    """Validate a CSS size: number (px) or a size string like '100%'."""
    if isinstance(value, bool):
        raise UIError(f"{prop} must be a number or CSS size string, not bool")
    if isinstance(value, (int, float)):
        if value <= 0:
            raise UIError(f"{prop} must be positive, got {value}")
        return f"{value}px"
    if isinstance(value, str):
        v = value.strip()
        if _CSS_SIZE_RE.match(v):
            return v
    raise UIError(
        f"{prop} must be a positive number (px) or a CSS size string "
        f"like '100%', '300px', '50vh' — got {value!r}"
    )


def _require_str(value, what: str) -> str:
    if not isinstance(value, str):
        raise UIError(f"{what} must be a str, got {type(value).__name__}")
    return value


def _require_bool(value, what: str) -> bool:
    if not isinstance(value, bool):
        raise UIError(f"{what} must be a bool, got {type(value).__name__}")
    return value


def _require_int(value, what: str) -> int:
    # bool is an int subclass — reject it explicitly.
    if not isinstance(value, int) or isinstance(value, bool):
        raise UIError(f"{what} must be an int, got {type(value).__name__}")
    return value


def _require_number(value, what: str):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise UIError(f"{what} must be an int or float, got {type(value).__name__}")
    import math

    if not math.isfinite(value):
        raise UIError(f"{what} must be finite, got {value!r}")
    return value


def _require_callable(value, what: str, error_type: type = UIError):
    if not callable(value):
        raise error_type(f"{what} must be callable, got {type(value).__name__}")
    return value


def _require_choice(value, choices: tuple, what: str):
    if value not in choices:
        raise UIError(f"{what} must be one of {list(choices)}, got {value!r}")
    return value


def _type_name_of(component) -> str:
    return type(component).__name__


def _duplicate_id_error(component_id: str, existing, conflicting) -> DuplicateComponentIdError:
    """Build a DuplicateComponentIdError carrying type+path of both sides."""
    return DuplicateComponentIdError(
        component_id=component_id,
        existing_type=_type_name_of(existing),
        existing_path=existing._path(),
        conflicting_type=_type_name_of(conflicting),
        conflicting_path=conflicting._path(),
    )


# ------------------------------------------------------------------- base
class UIComponent:
    """Common contract for every UI builder.

    Lifecycle: build -> (parent links maintained by containers) -> frozen
    by the registry on attachment. All setters validate immediately,
    return self for chaining, and refuse to run once frozen.
    """

    _type_name = "component"  # serialization name, overridden per class
    _INTERACTIVE = False  # True on components that carry actions

    def __init__(self, *, component_id: str = "", title: str | None = None, **kwargs) -> None:
        if component_id:
            self._validate_id(component_id)
        self._id = component_id or self._generate_id()
        self._title: str | None = _require_str(title, "title") if title is not None else None
        if kwargs:
            raise UIError(
                f"unknown constructor argument(s) {sorted(kwargs)} — "
                f"the builder API uses fluent setters"
            )
        self._check = None  # visibility check callable
        self._parent = None  # set by the owning parent on add
        self._frozen = False  # set by the registry on attachment
        self._disabled = False
        self._width: str | None = None  # CSS size ("300px", "100%", "50vh")
        self._height: str | None = None
        # side-by-side layout hints (applied when rendered inside a
        # Row/Column/Container): how much free space this child takes,
        # whether it may squeeze, and a cross-axis alignment override
        self._grow: int | None = None
        self._shrink: int | None = None
        self._align_self: str | None = None

    # --- fluent setters (return self) ---

    def set_id(self, component_id: str) -> UIComponent:
        """Change the id. Validated first; releases the old id with the
        parent and checks the new one against the current tree."""
        self._check_frozen("change the id")
        self._validate_id(component_id)
        new_id = component_id
        old_id = self._id
        if new_id == old_id:
            return self
        parent = self._parent
        if parent is not None:
            parent._check_id_available(new_id, excluding=self)
        self._id = new_id
        if parent is not None:
            parent._child_ids.discard(old_id)
            parent._child_ids.add(new_id)
        return self

    def set_title(self, title: str) -> UIComponent:
        self._check_frozen("set the title")
        self._title = _require_str(title, "title")
        return self

    def set_check(self, check) -> UIComponent:
        """Set the visibility check callable (evaluated server-side)."""
        self._check_frozen("set the check")
        self._check = _require_callable(check, "check")
        return self

    def set_disabled(self, disabled: bool) -> UIComponent:
        self._check_frozen("set disabled")
        self._disabled = _require_bool(disabled, "disabled")
        return self

    def set_width(self, width) -> UIComponent:
        """Set the component width (CSS size string or px number).

        Accepts: "300px", "100%", "50vw", "12rem", or a positive
        number (interpreted as pixels). Validated immediately.
        """
        self._check_frozen("set width")
        self._width = _validate_css_size(width, "width")
        return self

    def set_height(self, height) -> UIComponent:
        """Set the component height (CSS size string or px number)."""
        self._check_frozen("set height")
        self._height = _validate_css_size(height, "height")
        return self

    def set_grow(self, grow) -> UIComponent:
        """Side-by-side share: how eagerly this child takes free space.

        A flex-grow weight (0–100 int). 0 keeps the child at its natural/
        fixed width; equal weights split spare space evenly; 2 vs 1 gives
        the first child double. Complements ``set_width`` (a fixed width
        wins while space lasts; grow governs the surplus).
        """
        self._check_frozen("set grow")
        value = _require_int(grow, "grow")
        if not 0 <= value <= 100:
            raise UIError(f"grow must be 0-100, got {value}")
        self._grow = value
        return self

    def set_shrink(self, shrink) -> UIComponent:
        """Squeeze allowance (flex-shrink weight, 0–100; 0 = never)."""
        self._check_frozen("set shrink")
        value = _require_int(shrink, "shrink")
        if not 0 <= value <= 100:
            raise UIError(f"shrink must be 0-100, got {value}")
        self._shrink = value
        return self

    def set_align_self(self, align) -> UIComponent:
        """Override the PARENT's cross-axis alignment for this child only.

        One of "auto" (inherit), "start", "center", "end", "stretch",
        "baseline" — e.g. keep one button centered in a row whose other
        children align to the top.
        """
        self._check_frozen("set align_self")
        self._align_self = _require_choice(
            align, ("auto", "start", "center", "end", "stretch", "baseline"), "align_self"
        )
        return self

    # --- getters ---

    def get_id(self) -> str:
        return self._id

    def get_check(self):
        return self._check

    def get_parent(self):
        return self._parent

    # --- internal (used by parents/registry) ---

    def _generate_id(self) -> str:
        return uuid.uuid4().hex[:12]

    @staticmethod
    def _validate_id(component_id) -> None:
        if not isinstance(component_id, str):
            raise InvalidComponentIdError(
                f"component id must be a str, got {type(component_id).__name__}"
            )
        if not 1 <= len(component_id) <= _MAX_ID_LENGTH:
            raise InvalidComponentIdError(
                f"component id must be 1-{_MAX_ID_LENGTH} chars, got {len(component_id)}"
            )
        if not _ID_RE.match(component_id):
            raise InvalidComponentIdError(
                f"component id may only contain [a-zA-Z0-9_-], got {component_id!r}"
            )

    def _set_parent(self, parent) -> None:
        self._parent = parent

    def _detach(self) -> None:
        self._parent = None

    def _freeze(self) -> None:
        """Freeze this component and (for containers) its whole subtree."""
        self._frozen = True
        for child in getattr(self, "_children", ()):
            child._freeze()

    def _check_frozen(self, action: str) -> None:
        if self._frozen:
            raise FrozenLayoutError(
                f"cannot {action}: component '{self._id}' "
                f"({_type_name_of(self)}) is attached to a frozen layout"
            )

    def _path(self) -> str:
        """Slash-joined ids from the tree root down to this component."""
        parts: list[str] = []
        seen: set[int] = set()
        node = self
        while node is not None and id(node) not in seen:
            seen.add(id(node))
            parts.append(node.get_id())
            node = node.get_parent()
        return "/".join(reversed(parts))

    def _validate(self) -> list[str]:
        """Local, cheap invariant checks; tree checks live in Layout."""
        errors: list[str] = []
        try:
            self._validate_id(self._id)
        except InvalidComponentIdError as exc:
            errors.append(str(exc))
        if self._disabled not in (True, False):
            errors.append(f"component '{self._id}' has a non-bool disabled state")
        return errors

    # --- serialization ---

    def to_dict(self) -> dict:
        """JSON-safe dict: type, id, props (and children for containers)."""
        payload = {"type": self._type_name, "id": self._id}
        payload.update(self._props())
        return payload

    def _props(self) -> dict:
        props: dict = {}
        if self._title is not None:
            props["title"] = self._title
        if self._disabled:
            props["disabled"] = True
        if self._width is not None:
            props["width"] = self._width
        if self._height is not None:
            props["height"] = self._height
        if self._grow is not None:
            props["grow"] = self._grow
        if self._shrink is not None:
            props["shrink"] = self._shrink
        if self._align_self is not None:
            props["align_self"] = self._align_self
        return props

    def __repr__(self) -> str:
        parent_id = self._parent.get_id() if self._parent is not None else None
        return f"<{_type_name_of(self)} id={self._id!r} parent={parent_id!r} frozen={self._frozen}>"


# -------------------------------------------------------------- container
class UIContainer(UIComponent):
    """A component that can hold children.

    Subclasses pin `_allowed_child_types`; `_check_child` enforces it plus
    single-ownership, duplicate ids, self-reference and cycles before any
    mutation happens. Multi-child adds validate every child first, so a
    failing batch leaves the tree untouched.
    """

    _allowed_child_types: tuple = ()

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._children: list = []
        self._child_ids: set = set()

    # --- child validation / registration (internal) ---

    def _check_child(self, child, _replacing=None) -> None:
        """Validate a child before adding — raises on: wrong type,
        already-owned, duplicate ID, self-reference, cycle."""

        def _describe(component) -> str:
            if isinstance(component, UIComponent):
                return f"{_type_name_of(component)} '{component.get_id()}'"
            return repr(component)

        if not isinstance(child, self._allowed_child_types):
            allowed = ", ".join(t.__name__ for t in self._allowed_child_types) or "nothing"
            raise InvalidChildError(
                f"{_describe(child)} cannot be a child of "
                f"{_type_name_of(self)} '{self._id}' (allowed: {allowed})"
            )
        if child._parent is not None:
            raise InvalidChildError(
                f"component '{child.get_id()}' already has a parent "
                f"({_type_name_of(child._parent)} '{child._parent.get_id()}')"
            )
        if child is self:
            raise TreeValidationError(
                f"cannot add a component to itself ({_type_name_of(self)} '{self._id}')"
            )
        child_id = child.get_id()
        excluded = _replacing.get_id() if _replacing is not None else None
        if child_id != excluded and child_id in self._child_ids:
            existing = next((c for c in self._children if c.get_id() == child_id), None)
            if existing is not None:
                raise _duplicate_id_error(child_id, existing, child)
        # cycle check: walk up from self to see if child is an ancestor
        node = self
        while node is not None:
            if node is child:
                raise TreeValidationError(
                    f"cycle detected: {_type_name_of(child)} '{child.get_id()}' "
                    f"is an ancestor of {_type_name_of(self)} '{self._id}'"
                )
            node = node.get_parent()

    def _check_id_available(self, component_id: str, excluding=None) -> None:
        """Raise if component_id is taken among this container's children
        (used by UIComponent.set_id; skips the renaming component itself)."""
        if component_id not in self._child_ids:
            return
        existing = next((c for c in self._children if c.get_id() == component_id), None)
        if existing is not None and existing is not excluding:
            conflicting = excluding if excluding is not None else existing
            raise _duplicate_id_error(component_id, existing, conflicting)

    def _add_child(self, child) -> None:
        """After _check_child passes: set ownership, register ID."""
        child._set_parent(self)
        self._child_ids.add(child.get_id())
        self._children.append(child)

    def _add_many(self, children) -> None:
        """Atomic multi-add: validate ALL children (type, parent, duplicate,
        cycle — including duplicates inside the batch), then add them all."""
        self._check_frozen("add children")
        batch: dict[str, object] = {}
        for child in children:
            self._check_child(child)
            cid = child.get_id()
            if cid in batch:
                raise _duplicate_id_error(cid, batch[cid], child)
            batch[cid] = child
        for child in children:
            self._add_child(child)

    def _remove_child(self, child) -> None:
        child._detach()
        self._child_ids.discard(child.get_id())
        self._children.remove(child)

    def _remove_child_by_id(self, component_id: str, expected_types=None, kind: str = "child"):
        self._check_frozen(f"remove {kind} '{component_id}'")
        for child in self._children:
            if child.get_id() == component_id:
                if expected_types is not None and not isinstance(child, expected_types):
                    raise InvalidChildError(
                        f"component '{component_id}' is a {_type_name_of(child)}, "
                        f"not a {kind} of {_type_name_of(self)} '{self._id}'"
                    )
                self._remove_child(child)
                return self
        raise InvalidChildError(
            f"no {kind} with id '{component_id}' on {_type_name_of(self)} '{self._id}'"
        )

    # --- generic child access (typed add/remove live on subclasses) ---

    def get_child(self, component_id: str):
        child = next((c for c in self._children if c.get_id() == component_id), None)
        if child is None:
            raise InvalidChildError(
                f"no child with id '{component_id}' on {_type_name_of(self)} '{self._id}'"
            )
        return child

    def children(self) -> list:
        return list(self._children)

    def clear_children(self) -> UIContainer:
        self._check_frozen("clear children")
        for child in list(self._children):
            self._remove_child(child)
        return self

    def replace_child(self, component_id: str, comp) -> UIContainer:
        """Atomically swap the child with `component_id` for `comp`."""
        self._check_frozen(f"replace child '{component_id}'")
        old = self.get_child(component_id)
        self._check_child(comp, _replacing=old)
        self._remove_child(old)
        self._add_child(comp)
        return self

    def to_dict(self) -> dict:
        payload = super().to_dict()
        payload["children"] = [child.to_dict() for child in self._children]
        return payload


class _ComponentParent(UIContainer):
    """Shared add/remove API for the free-form content containers
    (Section, Card, Row, Column, Container); each pins its own allowed
    types.

    Every content container also carries the two-axis alignment
    vocabulary its children are laid out by (CSS flex semantics,
    whitelisted keywords only — never arbitrary style strings):

      * ``set_align``     — CROSS-axis (vertical inside a Row, horizontal
                            inside a Column/vertical container)
      * ``set_justify``   — MAIN-axis distribution (horizontal inside a
                            Row, vertical inside a Column)
    """

    _CROSS_AXIS = ("start", "center", "end", "stretch", "baseline")
    _MAIN_AXIS = ("start", "center", "end", "between", "around", "evenly")

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._align: str | None = None
        self._justify: str | None = None
        self._gap: str | None = None
        self._icon: str | None = None

    def set_gap(self, gap) -> _ComponentParent:
        """Spacing between children (CSS size string or px number)."""
        self._check_frozen("set the gap")
        self._gap = _validate_css_size(gap, "gap")
        return self

    _ICON_RE = __import__("re").compile(r"^[MmZzLlHhVvCcSsQqTtAaEe0-9 ,.\-+]{1,512}$")

    def set_icon(self, icon) -> _ComponentParent:
        """Decorative SVG path drawn beside the title (24×24 viewBox
        path data — narrow path-data charset, ≤512 chars; anything else
        is rejected so plugin data can never become arbitrary markup)."""
        self._check_frozen("set the icon")
        icon = _require_str(icon, "icon")
        if not self._ICON_RE.match(icon):
            raise UIError(
                "icon must be SVG path data (letters MZLHVCSQTA, digits, "
                "spaces, commas, dots, + and -)"
            )
        self._icon = icon
        return self

    def set_align(self, align) -> _ComponentParent:
        """Cross-axis alignment of the children ("start"|"center"|"end"|
        "stretch"|"baseline") — vertical in a Row, horizontal in a
        Column/vertical Container."""
        self._check_frozen("set align")
        self._align = _require_choice(align, self._CROSS_AXIS, "align")
        return self

    def set_justify(self, justify) -> _ComponentParent:
        """Main-axis distribution of the children ("start"|"center"|"end"|
        "between"|"around"|"evenly") — horizontal in a Row, vertical in a
        Column/vertical Container."""
        self._check_frozen("set justify")
        self._justify = _require_choice(justify, self._MAIN_AXIS, "justify")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._align is not None:
            props["align"] = self._align
        if self._justify is not None:
            props["justify"] = self._justify
        if self._gap is not None:
            props["gap"] = self._gap
        if self._icon is not None:
            props["icon"] = self._icon
        return props

    def add_component(self, comp) -> _ComponentParent:
        self._check_frozen("add a component")
        self._check_child(comp)
        self._add_child(comp)
        return self

    def add_components(self, *comps) -> _ComponentParent:
        self._add_many(comps)
        return self

    def remove_component(self, component_id: str) -> _ComponentParent:
        return self._remove_child_by_id(component_id, kind="component")


# ------------------------------------------------------- interactive parts
class _Interactive(UIComponent):
    """Base for components that register server-side actions."""

    _INTERACTIVE = True

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._action = None

    def set_action(self, action, require_account: bool = False) -> _Interactive:
        """Register the action callable.

        ``require_account=True`` marks the action ACCOUNT-SCOPED: the
        dashboard must supply a validated account (and dispatch receives
        that account's services facade) or the action is rejected — the
        account is never guessed.
        """
        self._check_frozen("set the action")
        self._action = _require_callable(action, "action", ActionRegistrationError)
        self._action_require_account = _require_bool(require_account, "require_account")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._action is not None:
            # The registry maps this component's id to the callable
            # server-side; the frontend only sees that an action exists.
            props["action"] = True
        return props

    def _validate(self) -> list[str]:
        errors = super()._validate()
        if self._action is not None and not callable(self._action):
            errors.append(
                f"interactive component '{self._id}' ({_type_name_of(self)}) "
                f"has a non-callable action"
            )
        return errors


class _InputField(_Interactive):
    """Shared field scaffolding: label, description, placeholder, required,
    default value. Select-style and text-style inputs refine it."""

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._label: str | None = None
        self._description: str | None = None
        self._placeholder: str | None = None
        self._required = False
        self._value = None

    def set_label(self, label: str) -> _InputField:
        self._check_frozen("set the label")
        self._label = _require_str(label, "label")
        return self

    def set_description(self, description: str) -> _InputField:
        self._check_frozen("set the description")
        self._description = _require_str(description, "description")
        return self

    def set_placeholder(self, placeholder: str) -> _InputField:
        self._check_frozen("set the placeholder")
        self._placeholder = _require_str(placeholder, "placeholder")
        return self

    def set_required(self, required: bool) -> _InputField:
        self._check_frozen("set required")
        self._required = _require_bool(required, "required")
        return self

    def set_value(self, value: str) -> _InputField:
        self._check_frozen("set the value")
        self._value = _require_str(value, "value")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._label is not None:
            props["label"] = self._label
        if self._description is not None:
            props["description"] = self._description
        if self._placeholder is not None:
            props["placeholder"] = self._placeholder
        if self._required:
            props["required"] = True
        if self._value is not None:
            props["value"] = self._value
        return props


# ---------------------------------------------------- display components
