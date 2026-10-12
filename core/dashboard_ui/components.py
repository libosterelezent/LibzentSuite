"""The dashboard UI component catalog.

Every concrete builder except the Layout root: display parts (Text,
Badge, Metric), the eight input types (Checkbox/Toggle, TextInput and
specializations, Select and variants), data parts (ListView, Table,
Progress), the Button action component, and the containers that hold
them (Form, Section, Card, Row, Column, Tab, Page). Package-internal:
import the public classes from core.dashboard_ui (or
core.dashboard_ui.builders), not from here.
"""
from __future__ import annotations

from ._base import (
    _ComponentParent,
    _InputField,
    _Interactive,
    _require_bool,
    _require_callable,
    _require_choice,
    _require_int,
    _require_number,
    _require_str,
    _validate_css_size,
    UIComponent,
    UIContainer,
)
from .errors import ActionRegistrationError, UIError

__all__ = [
    "Page",
    "Tab",
    "Section",
    "Card",
    "Row",
    "Column",
    "Text",
    "Badge",
    "Metric",
    "Checkbox",
    "Toggle",
    "TextInput",
    "NumberInput",
    "Textarea",
    "Select",
    "RadioGroup",
    "MultiSelect",
    "ListView",
    "Table",
    "Progress",
    "Button",
    "Form",
]


class Text(UIComponent):
    """A block of text."""

    _VARIANTS = ("body", "muted", "code", "heading")
    _type_name = "text"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._value: str | None = None
        self._variant: str | None = None
        self._data_source = None

    def set_value(self, text: str) -> "Text":
        self._check_frozen("set the value")
        self._value = _require_str(text, "value")
        return self

    def set_variant(self, variant: str) -> "Text":
        self._check_frozen("set the variant")
        self._variant = _require_choice(variant, self._VARIANTS, "variant")
        return self

    def set_data_source(self, data_source) -> "Text":
        """Live text: a provider returning ``{"value": str}`` (or a str).

        The rendered element carries the live flag; the frontend refreshes
        its content from the provider without rebuilding the DOM — the
        builder-based replacement for streaming panels (log tails, report
        viewers).
        """
        self._check_frozen("set the data source")
        self._data_source = _require_callable(data_source, "data_source")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._value is not None:
            props["value"] = self._value
        if self._variant is not None:
            props["variant"] = self._variant
        if getattr(self, "_data_source", None) is not None:
            props["live"] = True
        return props


class Badge(UIComponent):
    """A small status pill."""

    _VARIANTS = ("info", "ok", "warn", "err")
    _type_name = "badge"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._value: str | None = None
        self._variant: str | None = None

    def set_value(self, text: str) -> "Badge":
        self._check_frozen("set the value")
        self._value = _require_str(text, "value")
        return self

    def set_variant(self, variant: str) -> "Badge":
        self._check_frozen("set the variant")
        self._variant = _require_choice(variant, self._VARIANTS, "variant")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._value is not None:
            props["value"] = self._value
        if self._variant is not None:
            props["variant"] = self._variant
        return props


class Metric(UIComponent):
    """A label/value/sub readout with an optional level."""

    _LEVELS = ("", "ok", "warn", "err")
    _type_name = "metric"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._label: str | None = None
        self._value: str | None = None
        self._sub: str | None = None
        self._level: str | None = None
        self._data_source = None

    def set_label(self, label: str) -> "Metric":
        self._check_frozen("set the label")
        self._label = _require_str(label, "label")
        return self

    def set_value(self, value: str) -> "Metric":
        self._check_frozen("set the value")
        self._value = _require_str(value, "value")
        return self

    def set_sub(self, sub: str) -> "Metric":
        self._check_frozen("set the sub label")
        self._sub = _require_str(sub, "sub")
        return self

    def set_level(self, level: str) -> "Metric":
        self._check_frozen("set the level")
        self._level = _require_choice(level, self._LEVELS, "level")
        return self

    def set_data_source(self, data_source) -> "Metric":
        """Live values: a provider returning ``{value, sub?, level?}``.

        The rendered snapshot keeps whatever was set via ``set_value``;
        the frontend re-fetches the provider and updates the value, sub
        and level in place (no DOM rebuild) on every refresh cycle.
        """
        self._check_frozen("set the data source")
        self._data_source = _require_callable(data_source, "data_source")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._label is not None:
            props["label"] = self._label
        if self._value is not None:
            props["value"] = self._value
        if self._sub is not None:
            props["sub"] = self._sub
        if self._level is not None:
            props["level"] = self._level
        if getattr(self, "_data_source", None) is not None:
            props["live"] = True
        return props


# ----------------------------------------------------- input components
class Separator(UIComponent):
    """A horizontal divider line."""

    _type_name = "separator"

    def set_variant(self, variant: str) -> "Separator":
        """'solid' (default) or 'dashed'."""
        self._check_frozen("set the variant")
        self._variant = _require_choice(variant, ("solid", "dashed"), "variant")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if getattr(self, "_variant", None):
            props["variant"] = self._variant
        return props


class Description(UIComponent):
    """A descriptive paragraph (rendered smaller/muted below a title)."""

    _type_name = "description"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._value: str | None = None

    def set_value(self, text: str) -> "Description":
        self._check_frozen("set the value")
        self._value = _require_str(text, "value")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._value is not None:
            props["value"] = self._value
        return props


class Header(UIComponent):
    """A prominent section header with optional subtitle."""

    _type_name = "header"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._subtitle: str | None = None

    def set_subtitle(self, text: str) -> "Header":
        self._check_frozen("set the subtitle")
        self._subtitle = _require_str(text, "subtitle")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._subtitle is not None:
            props["subtitle"] = self._subtitle
        return props


class Footer(UIComponent):
    """A muted footer line (small text at the bottom of a section)."""

    _type_name = "footer"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._value: str | None = None

    def set_value(self, text: str) -> "Footer":
        self._check_frozen("set the value")
        self._value = _require_str(text, "value")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._value is not None:
            props["value"] = self._value
        return props


class Checkbox(_InputField):
    """A labeled boolean checkbox with an optional change action."""

    _type_name = "checkbox"

    def set_value(self, value: bool) -> "Checkbox":
        self._check_frozen("set the value")
        self._value = _require_bool(value, "value")
        return self


class Toggle(Checkbox):
    """Visual variant of Checkbox (switch styling)."""

    _type_name = "toggle"


class TextInput(_InputField):
    """A single-line text field."""

    _type_name = "text-input"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._max_length: int | None = None

    def set_max_length(self, max_length: int) -> "TextInput":
        self._check_frozen("set the max length")
        self._max_length = _require_int(max_length, "max_length")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._max_length is not None:
            props["max_length"] = self._max_length
        return props


class NumberInput(TextInput):
    """A numeric field with optional bounds and step."""

    _type_name = "number-input"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._min_value = None
        self._max_value = None
        self._step = None

    def set_min_value(self, min_value) -> "NumberInput":
        self._check_frozen("set the min value")
        self._min_value = _require_number(min_value, "min_value")
        return self

    def set_max_value(self, max_value) -> "NumberInput":
        self._check_frozen("set the max value")
        self._max_value = _require_number(max_value, "max_value")
        return self

    def set_step(self, step) -> "NumberInput":
        self._check_frozen("set the step")
        self._step = _require_number(step, "step")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._min_value is not None:
            props["min_value"] = self._min_value
        if self._max_value is not None:
            props["max_value"] = self._max_value
        if self._step is not None:
            props["step"] = self._step
        return props


class Textarea(TextInput):
    """A multi-line text field."""

    _type_name = "textarea"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._rows: int | None = None

    def set_rows(self, rows: int) -> "Textarea":
        self._check_frozen("set the rows")
        self._rows = _require_int(rows, "rows")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._rows is not None:
            props["rows"] = self._rows
        return props


class Select(_InputField):
    """A single-choice dropdown."""

    _type_name = "select"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._options: list | None = None

    def set_options(self, options) -> "Select":
        """Accepts a list of strings or {'value','label'} dicts; both are
        normalized to {'value','label'} dicts for serialization."""
        self._check_frozen("set the options")
        if not isinstance(options, list):
            raise UIError(f"options must be a list, got {type(options).__name__}")
        normalized: list[dict] = []
        for item in options:
            if isinstance(item, str):
                normalized.append({"value": item, "label": item})
            elif isinstance(item, dict) and "value" in item and "label" in item:
                normalized.append({"value": item["value"], "label": item["label"]})
            else:
                raise UIError(
                    "options entries must be strings or dicts with "
                    f"'value' and 'label' keys, got {item!r}")
        self._options = normalized
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._options is not None:
            props["options"] = [dict(o) for o in self._options]
        return props


class RadioGroup(Select):
    """Single-choice rendered as a radio group."""

    _type_name = "radio-group"


class MultiSelect(Select):
    """Multi-choice select; the value is a list."""

    _type_name = "multi-select"

    def set_value(self, value) -> "MultiSelect":
        self._check_frozen("set the value")
        if not isinstance(value, list):
            raise UIError(f"value must be a list, got {type(value).__name__}")
        self._value = value
        return self


# ------------------------------------------------- display data components
class ListView(_Interactive):
    """A dynamic list fed by a server-side data source callable."""

    _type_name = "list-view"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._data_source = None
        self._empty_text: str | None = None

    def set_data_source(self, data_source) -> "ListView":
        self._check_frozen("set the data source")
        self._data_source = _require_callable(data_source, "data_source")
        return self

    def set_empty_text(self, text: str) -> "ListView":
        self._check_frozen("set the empty text")
        self._empty_text = _require_str(text, "empty_text")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._empty_text is not None:
            props["empty_text"] = self._empty_text
        return props


class RowAction:
    """One per-row button on a :class:`Table`.

    ``action`` is called as ``action(row_key, context)`` where ``row_key``
    is the row's value in the table's key column and ``context`` is the
    :class:`~core.dashboard_ui.context.ActionContext` of the dispatch. It
    may be sync or async and returns anything an action callback may
    (None, str, dict, ActionResult).
    """

    __slots__ = ("id", "label", "action", "variant", "confirm")

    def __init__(self, action_id: str, label: str, action, *,
                 variant: str = "secondary", confirm: str = "") -> None:
        if not isinstance(action_id, str) or not action_id:
            raise UIError(f"row action id must be a non-empty str, got {action_id!r}")
        if not isinstance(label, str) or not label:
            raise UIError(f"row action label must be a non-empty str, got {label!r}")
        if not callable(action):
            raise UIError(
                f"row action {action_id!r} needs a callable action, got "
                f"{type(action).__name__}")
        if variant not in ("primary", "secondary", "danger", "ghost"):
            raise UIError(f"row action variant must be a known variant, got {variant!r}")
        if not isinstance(confirm, str):
            raise UIError(f"row action confirm must be a str, got {confirm!r}")
        self.id = action_id
        self.label = label
        self.action = action
        self.variant = variant
        self.confirm = confirm


class Table(_Interactive):
    """A paginated table fed by a server-side data source callable.

    Tables may declare ROW ACTIONS: per-row buttons (edit/clear/reload…)
    dispatched through the table's own action channel with
    ``payload = {"action": <row action id>, "row": <key column value>}``.
    The key column tells both the renderer and the callbacks which column
    identifies a row.
    """

    _type_name = "table"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._columns: list[str] | None = None
        self._data_source = None
        self._empty_text: str | None = None
        self._page_size: int | None = None
        self._row_actions: list[RowAction] = []
        self._key_column: str | None = None

    def set_columns(self, columns) -> "Table":
        self._check_frozen("set the columns")
        if not isinstance(columns, list):
            raise UIError(f"columns must be a list, got {type(columns).__name__}")
        for column in columns:
            _require_str(column, "each column")
        self._columns = list(columns)
        return self

    def set_data_source(self, data_source) -> "Table":
        self._check_frozen("set the data source")
        self._data_source = _require_callable(data_source, "data_source")
        return self

    def set_empty_text(self, text: str) -> "Table":
        self._check_frozen("set the empty text")
        self._empty_text = _require_str(text, "empty_text")
        return self

    def set_page_size(self, page_size: int) -> "Table":
        self._check_frozen("set the page size")
        self._page_size = _require_int(page_size, "page_size")
        return self

    def set_key_column(self, column: str) -> "Table":
        """Which column identifies a row for row-action dispatch."""
        self._check_frozen("set the key column")
        self._key_column = _require_str(column, "key column")
        return self

    def set_row_actions(self, actions) -> "Table":
        """Declare per-row buttons (list of :class:`RowAction`)."""
        self._check_frozen("set the row actions")
        if not isinstance(actions, list):
            raise UIError(
                f"row actions must be a list, got {type(actions).__name__}")
        checked: list[RowAction] = []
        seen: set[str] = set()
        for entry in actions:
            if not isinstance(entry, RowAction):
                raise UIError(
                    "each row action must be a RowAction, got "
                    f"{type(entry).__name__}")
            if entry.id in seen:
                raise UIError(f"duplicate row action id {entry.id!r}")
            seen.add(entry.id)
            checked.append(entry)
        self._row_actions = checked
        if checked:
            # row buttons dispatch through the table's own action channel
            # with {"action": <id>, "row": <key>}; the dispatcher routes
            # to the right RowAction callable below.
            self.set_action(self._dispatch_row_action)
        return self

    async def _dispatch_row_action(self, context, payload):
        payload = payload or {}
        action_id = payload.get("action")
        row = payload.get("row")
        if not isinstance(action_id, str) or not action_id:
            raise UIError("row action dispatch needs an 'action' id")
        if not isinstance(row, str) or not row:
            raise UIError("row action dispatch needs a 'row' key")
        for entry in self._row_actions:
            if entry.id == action_id:
                result = entry.action(row, context)
                if hasattr(result, "__await__"):
                    result = await result
                return result
        raise UIError(f"unknown row action {action_id!r}")

    def _props(self) -> dict:
        props = super()._props()
        if self._columns is not None:
            props["columns"] = list(self._columns)
        if self._empty_text is not None:
            props["empty_text"] = self._empty_text
        if self._page_size is not None:
            props["page_size"] = self._page_size
        if self._key_column is not None:
            props["key_column"] = self._key_column
        if self._row_actions:
            props["row_actions"] = [
                {"id": a.id, "label": a.label,
                 "variant": a.variant, "confirm": a.confirm}
                for a in self._row_actions
            ]
        return props


class Progress(UIComponent):
    """A 0-100 progress indicator."""

    _type_name = "progress"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._value = None
        self._label: str | None = None

    def set_value(self, value) -> "Progress":
        self._check_frozen("set the value")
        value = _require_number(value, "value")
        if not 0 <= value <= 100:
            raise UIError(f"value must be between 0 and 100, got {value}")
        self._value = value
        return self

    def set_label(self, label: str) -> "Progress":
        self._check_frozen("set the label")
        self._label = _require_str(label, "label")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._value is not None:
            props["value"] = self._value
        if self._label is not None:
            props["label"] = self._label
        return props


# ------------------------------------------------------- action components
class Button(_Interactive):
    """A clickable action with an optional confirmation and loading state."""

    _VARIANTS = ("primary", "secondary", "danger", "ghost")
    _type_name = "button"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._label: str | None = None
        self._description: str | None = None
        self._variant: str | None = None
        self._confirm: str | None = None
        self._loading = False

    def set_label(self, label: str) -> "Button":
        self._check_frozen("set the label")
        self._label = _require_str(label, "label")
        return self

    def set_description(self, description: str) -> "Button":
        self._check_frozen("set the description")
        self._description = _require_str(description, "description")
        return self

    def set_variant(self, variant: str) -> "Button":
        self._check_frozen("set the variant")
        self._variant = _require_choice(variant, self._VARIANTS, "variant")
        return self

    def set_confirm(self, confirm: str) -> "Button":
        """Require the user to confirm with this message before firing."""
        self._check_frozen("set the confirmation")
        self._confirm = _require_str(confirm, "confirm")
        return self

    def set_loading(self, loading: bool) -> "Button":
        self._check_frozen("set loading")
        self._loading = _require_bool(loading, "loading")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._label is not None:
            props["label"] = self._label
        if self._description is not None:
            props["description"] = self._description
        if self._variant is not None:
            props["variant"] = self._variant
        if self._confirm is not None:
            props["confirm"] = self._confirm
        if self._loading:
            props["loading"] = True
        return props


class Form(UIContainer):
    """A labeled group of input fields with a submit action."""

    _type_name = "form"
    _allowed_child_types = ()  # pinned after the field classes exist

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._submit_action = None
        self._submit_label: str | None = None
        self._client_action: str | None = None

    def set_submit_action(self, action) -> "Form":
        self._check_frozen("set the submit action")
        self._submit_action = _require_callable(
            action, "submit action", ActionRegistrationError)
        return self

    def set_submit_label(self, label: str) -> "Form":
        self._check_frozen("set the submit label")
        self._submit_label = _require_str(label, "submit label")
        return self

    def set_description(self, description: str) -> "Form":
        self._check_frozen("set the description")
        self._description = _require_str(description, "description")
        return self

    _CLIENT_ACTIONS = ("save-token", "save-theme")

    def set_client_action(self, name: str) -> "Form":
        """Handle submission CLIENT-side instead of a server action.

        Supported names: ``"save-token"`` — the dashboard's own session
        token editor (the token must never round-trip a server action);
        ``"save-theme"`` — the decorative theme picker (pure
        presentation, persisted in the browser only). The renderer
        collects the form's fields exactly like a server submit, then
        applies the client behavior.
        """
        self._check_frozen("set the client action")
        if name not in self._CLIENT_ACTIONS:
            raise UIError(
                f"unknown client action {name!r} "
                f"(one of {', '.join(self._CLIENT_ACTIONS)})")
        self._client_action = name
        return self

    def add_field(self, field) -> "Form":
        self._check_frozen("add a field")
        self._check_child(field)
        self._add_child(field)
        return self

    def add_fields(self, *fields) -> "Form":
        self._add_many(fields)
        return self

    def remove_field(self, component_id: str) -> "Form":
        return self._remove_child_by_id(
            component_id, expected_types=self._allowed_child_types, kind="field")

    def fields(self) -> list:
        return list(self._children)

    def _props(self) -> dict:
        props = super()._props()
        if self._submit_label is not None:
            props["submit_label"] = self._submit_label
        if self._submit_action is not None:
            props["submit_action"] = True
        if self._client_action is not None:
            props["client_action"] = self._client_action
        if getattr(self, "_description", None) is not None:
            props["description"] = self._description
        return props

    def _validate(self) -> list[str]:
        errors = super()._validate()
        if self._submit_action is not None and not callable(self._submit_action):
            errors.append(f"form '{self._id}' has a non-callable submit action")
        return errors


class Container(_ComponentParent):
    """A neutral, untitled grouping box — the alignment primitive.

    Unlike Section (titled block) or Card (boxed panel), a Container
    adds no chrome: it exists to GROUP children and own their layout.
    ``set_direction`` picks the flow axis; ``set_align``/``set_justify``
    control the two axes; children claim space with ``set_grow``.
    Containers may nest (a Container inside a Container is explicit
    opt-in nesting — the Row-inside-Row prohibition still holds).
    """

    _type_name = "container"
    _DIRECTIONS = ("vertical", "horizontal")

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._direction: str | None = None

    def set_direction(self, direction) -> "Container":
        """Which way children flow ("vertical" default | "horizontal")."""
        self._check_frozen("set the direction")
        self._direction = _require_choice(direction, self._DIRECTIONS, "direction")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._direction is not None:
            props["direction"] = self._direction
        return props


class Grid(_ComponentParent):
    """A structured column grid — THE layout primitive for tidiness.

    Children slot into equal-width columns that wrap as whole rows
    (unlike a Row, where mixed-height children raggedly re-flow).
    ``set_columns(n)`` pins 1–6 columns; ``set_columns("auto")`` plus
    ``set_min_child("240px")`` lets the renderer fit as many columns
    as the width allows (auto-fill). Narrow screens collapse via CSS.
    """

    _type_name = "grid"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._columns = None          # int 1-6 or "auto"
        self._min_child: str | None = None   # css size (auto mode)

    def set_columns(self, columns) -> "Grid":
        """Column count (int 1–6) or "auto" (with set_min_child)."""
        self._check_frozen("set the columns")
        if columns == "auto":
            self._columns = "auto"
            return self
        value = _require_int(columns, "columns")
        if not 1 <= value <= 6:
            raise UIError(f"columns must be 1-6 (or 'auto'), got {value}")
        self._columns = value
        return self

    def set_min_child(self, min_child) -> "Grid":
        """Minimum child width in auto mode (CSS size; default 240px)."""
        self._check_frozen("set the min child")
        self._min_child = _validate_css_size(min_child, "min_child")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._columns is not None:
            props["columns"] = self._columns
        if self._min_child is not None:
            props["min_child"] = self._min_child
        return props


class Alert(UIComponent):
    """A message box for errors, warnings and notices.

    The designed home for FAILURE: plugins surface caught errors as an
    Alert instead of raw text, and the generic renderer uses the same
    markup when a data provider or page fetch fails — so problems read
    as part of the UI, never as a glitch that breaks the layout.
    """

    _type_name = "alert"
    _VARIANTS = ("info", "ok", "warn", "err")

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._value: str | None = None
        self._variant: str | None = None

    def set_value(self, value) -> "Alert":
        """The message body."""
        self._check_frozen("set the value")
        self._value = _require_str(value, "value")
        return self

    def set_variant(self, variant: str) -> "Alert":
        """Severity look + icon ("info"|"ok"|"warn"|"err")."""
        self._check_frozen("set the variant")
        self._variant = _require_choice(variant, self._VARIANTS, "variant")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._value is not None:
            props["value"] = self._value
        if self._variant is not None:
            props["variant"] = self._variant
        return props


class DisplayText(UIComponent):
    """Read-only presentation text with tone, size, weight and alignment.

    Distinct from ``Text`` (semantic body copy with variants):
    DisplayText is for VALUES you want to present with visual weight —
    a headline number, a status word, a right-aligned figure — and it
    participates in side-by-side layouts like any component.
    """

    _type_name = "display-text"
    _TONES = ("default", "dim", "ok", "warn", "err")
    _SIZES = ("xs", "sm", "md", "lg", "xl")
    _WEIGHTS = ("normal", "medium", "semibold", "bold")
    _ALIGNS = ("left", "center", "right")

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._value: str | None = None
        self._tone: str | None = None
        self._size: str | None = None
        self._weight: str | None = None
        self._align: str | None = None
        self._monospace: bool | None = None

    def set_value(self, value) -> "DisplayText":
        self._check_frozen("set the value")
        self._value = _require_str(value, "value")
        return self

    def set_tone(self, tone: str) -> "DisplayText":
        """Color intent ("default"|"dim"|"ok"|"warn"|"err")."""
        self._check_frozen("set the tone")
        self._tone = _require_choice(tone, self._TONES, "tone")
        return self

    def set_size(self, size: str) -> "DisplayText":
        """Type scale ("xs"|"sm"|"md"|"lg"|"xl")."""
        self._check_frozen("set the size")
        self._size = _require_choice(size, self._SIZES, "size")
        return self

    def set_weight(self, weight: str) -> "DisplayText":
        """Font weight ("normal"|"medium"|"semibold"|"bold")."""
        self._check_frozen("set the weight")
        self._weight = _require_choice(weight, self._WEIGHTS, "weight")
        return self

    def set_align(self, align: str) -> "DisplayText":
        """Self alignment of the text ("left"|"center"|"right")."""
        self._check_frozen("set the align")
        self._align = _require_choice(align, self._ALIGNS, "align")
        return self

    def set_monospace(self, monospace: bool) -> "DisplayText":
        self._check_frozen("set monospace")
        self._monospace = _require_bool(monospace, "monospace")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._value is not None:
            props["value"] = self._value
        if self._tone is not None:
            props["tone"] = self._tone
        if self._size is not None:
            props["size"] = self._size
        if self._weight is not None:
            props["weight"] = self._weight
        if self._align is not None:
            props["align"] = self._align
        if self._monospace is not None:
            props["monospace"] = self._monospace
        return props


# Pin the form's field types now that every field class exists.
_FIELD_TYPES = (TextInput, NumberInput, Textarea, Select, RadioGroup,
                MultiSelect, Checkbox, Toggle)
Form._allowed_child_types = _FIELD_TYPES


class Section(_ComponentParent):
    """A titled block inside a Page/Tab holding any content component."""

    _type_name = "section"

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._description: str | None = None
        self._icon: str | None = None

    def set_icon(self, icon) -> "Section":
        """Decorative SVG path drawn beside the title (24×24 viewBox
        path data, same vocabulary as the sidebar icons). Escaped by
        the renderer; purely decorative."""
        self._check_frozen("set the icon")
        self._icon = _require_str(icon, "icon")
        return self

    def set_description(self, description: str) -> "Section":
        self._check_frozen("set the description")
        self._description = _require_str(description, "description")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._description is not None:
            props["description"] = self._description
        if self._icon is not None:
            props["icon"] = self._icon
        return props


class Card(_ComponentParent):
    """A boxed content container with an optional title.

    ``set_tone`` adds a decorative accent border (info/ok/warn/err) —
    emphasis ONLY: tone never changes meaning, order or layout.
    """

    _type_name = "card"
    _TONES = ("info", "ok", "warn", "err")

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._tone: str | None = None

    def set_tone(self, tone) -> "Card":
        """Decorative accent border ("info"|"ok"|"warn"|"err")."""
        self._check_frozen("set the tone")
        self._tone = _require_choice(tone, self._TONES, "tone")
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._tone is not None:
            props["tone"] = self._tone
        return props


class Row(_ComponentParent):
    """Horizontal layout container (no nested rows)."""

    _type_name = "row"


class Column(_ComponentParent):
    """Vertical layout container (no nested rows)."""

    _type_name = "column"


# Pin the content containers' child types now that they all exist:
# Section/Card accept every content component; Row/Column accept the same
# set minus Row (no nested rows); Container additionally accepts other
# Containers (explicit opt-in nesting — see its docstring).
_CONTENT_CHILD_TYPES = (Card, Row, Column, Container, Grid, Alert,
                        Text, DisplayText, Badge, Metric, Button,
                        Checkbox, Toggle, TextInput, NumberInput, Textarea,
                        Select, RadioGroup, MultiSelect, ListView, Table,
                        Progress, Form, Separator, Description, Header, Footer)
_FLOW_CHILD_TYPES = tuple(t for t in _CONTENT_CHILD_TYPES if t is not Row)
Section._allowed_child_types = _CONTENT_CHILD_TYPES
Card._allowed_child_types = _CONTENT_CHILD_TYPES
Row._allowed_child_types = _FLOW_CHILD_TYPES
Column._allowed_child_types = _FLOW_CHILD_TYPES
Container._allowed_child_types = _FLOW_CHILD_TYPES   # incl. Containers: opt-in nesting
Grid._allowed_child_types = _FLOW_CHILD_TYPES


class Tab(UIContainer):
    """A tab page inside a Page; holds sections."""

    _type_name = "tab"
    _allowed_child_types = (Section,)

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._icon: str | None = None

    def set_icon(self, icon: str) -> "Tab":
        self._check_frozen("set the icon")
        self._icon = _require_str(icon, "icon")
        return self

    def add_section(self, section) -> "Tab":
        self._check_frozen("add a section")
        self._check_child(section)
        self._add_child(section)
        return self

    def add_sections(self, *sections) -> "Tab":
        self._add_many(sections)
        return self

    def _props(self) -> dict:
        props = super()._props()
        if self._icon is not None:
            props["icon"] = self._icon
        return props


class Page(UIContainer):
    """A dashboard page holding sections and/or tabs."""

    _type_name = "page"
    _allowed_child_types = (Section, Tab)

    def __init__(self, *, component_id: str = "", **kwargs) -> None:
        super().__init__(component_id=component_id, **kwargs)
        self._description: str | None = None
        self._icon: str | None = None
        self._order: int | None = None

    def set_description(self, description: str) -> "Page":
        self._check_frozen("set the description")
        self._description = _require_str(description, "description")
        return self

    def set_icon(self, icon: str) -> "Page":
        self._check_frozen("set the icon")
        self._icon = _require_str(icon, "icon")
        return self

    def set_order(self, order: int) -> "Page":
        self._check_frozen("set the order")
        self._order = _require_int(order, "order")
        return self

    def add_section(self, section) -> "Page":
        self._check_frozen("add a section")
        self._check_child(section)
        self._add_child(section)
        return self

    def add_sections(self, *sections) -> "Page":
        self._add_many(sections)
        return self

    def add_tab(self, tab) -> "Page":
        self._check_frozen("add a tab")
        self._check_child(tab)
        self._add_child(tab)
        return self

    def add_tabs(self, *tabs) -> "Page":
        self._add_many(tabs)
        return self

    def remove_section(self, component_id: str) -> "Page":
        return self._remove_child_by_id(component_id, expected_types=(Section,), kind="section")

    def remove_tab(self, component_id: str) -> "Page":
        return self._remove_child_by_id(component_id, expected_types=(Tab,), kind="tab")

    def sections(self) -> list:
        return [c for c in self._children if isinstance(c, Section)]

    def tabs(self) -> list:
        return [c for c in self._children if isinstance(c, Tab)]

    def _props(self) -> dict:
        props = super()._props()
        if self._description is not None:
            props["description"] = self._description
        if self._icon is not None:
            props["icon"] = self._icon
        if self._order is not None:
            props["order"] = self._order
        return props


