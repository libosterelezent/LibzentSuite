"""Dashboard UI builder core: fluent, validated, JSON-serializable components.

Stage B of the dashboard UI API. Pure core — stdlib only, no imports from
adapters/, interfaces/, database/ or the dashboard itself. This module is
the canonical public surface of the package: it defines the Layout root
and the DashboardUIToolset namespace and re-exports the whole catalog
(implementation lives in _base.py and components.py).

Design guarantees:

  * fluent builders for the whole catalog (Layout -> Page/Tab ->
    Section/Card/Row/Column -> display, input, data and action parts);
  * id management: uuid4-hex[:12] generated ids, validated explicit ids
    ([A-Za-z0-9_-]{1,64}), parent-level duplicate detection on add and
    layout-level tree validation for everything else;
  * cycle prevention on every add (ancestor walk) and atomic multi-child
    adds (all-or-nothing);
  * type-checked setters that never corrupt state on failure;
  * JSON-safe to_dict() serialization — actions surface as
    {"action": True} (never the callable; the server-side registry maps
    component ids back to callables) and no parent/internal refs leak.
"""

from __future__ import annotations

from collections import deque

from ._base import UIComponent, UIContainer, _type_name_of
from .components import (
    Alert,
    Badge,
    BulkAction,
    Button,
    Card,
    Checkbox,
    Column,
    Container,
    Description,
    DisplayText,
    Footer,
    Form,
    Grid,
    Header,
    ListView,
    Metric,
    Modal,
    MultiSelect,
    NumberInput,
    Page,
    Progress,
    RadioGroup,
    Row,
    RowAction,
    Section,
    Select,
    Separator,
    Tab,
    Table,
    Text,
    Textarea,
    TextInput,
    TileView,
    Toggle,
    Toolbar,
)
from .errors import (
    ActionRegistrationError,
    DuplicateComponentIdError,
    FrozenLayoutError,
    InvalidChildError,
    InvalidComponentIdError,
    TreeValidationError,
    UIError,
)

__all__ = [
    "ActionRegistrationError",
    "Badge",
    "Button",
    "Card",
    "Checkbox",
    "Column",
    "DashboardUIToolset",
    "DuplicateComponentIdError",
    "Form",
    "FrozenLayoutError",
    "InvalidChildError",
    "InvalidComponentIdError",
    "Layout",
    "ListView",
    "Metric",
    "MultiSelect",
    "NumberInput",
    "Page",
    "Progress",
    "RadioGroup",
    "Row",
    "Section",
    "Select",
    "Tab",
    "Table",
    "Text",
    "TextInput",
    "Textarea",
    "Toggle",
    "TreeValidationError",
    "UIComponent",
    "UIContainer",
    "UIError",
]


class Layout(UIContainer):
    """Root of a dashboard UI tree; holds pages."""

    _type_name = "layout"
    _allowed_child_types = (Page,)

    def add_page(self, page) -> Layout:
        self._check_frozen("add a page")
        self._check_child(page)
        self._add_child(page)
        return self

    def add_pages(self, *pages) -> Layout:
        self._add_many(pages)
        return self

    def remove_page(self, component_id: str) -> Layout:
        return self._remove_child_by_id(component_id, expected_types=(Page,), kind="page")

    def pages(self) -> list:
        return [c for c in self._children if isinstance(c, Page)]

    def validate(self) -> list[str]:
        """Validate the whole tree; returns a list of error strings
        ([] when valid). Never raises on tree problems."""
        return self._validate_tree()

    #: resource limits (spec 4.1): an accidental or hostile definition
    #: cannot build an enormous tree or uncontrolled recursive depth
    MAX_TREE_COMPONENTS = 1000
    MAX_TREE_DEPTH = 24

    def _validate_tree(self) -> list[str]:
        """BFS the tree collecting structural problems: duplicate ids
        (tree-wide), children of the wrong type, mislinked/orphaned
        parent pointers, shared parents/cycles, broken actions, and
        size/depth limits."""
        errors: list[str] = []
        seen_ids: dict[str, str] = {}
        seen_objs: set[int] = set()
        queue: deque = deque([(self, self.get_id(), 0)])
        while queue:
            node, path, depth = queue.popleft()
            if depth > self.MAX_TREE_DEPTH:
                errors.append(f"tree nests deeper than {self.MAX_TREE_DEPTH} levels (at '{path}')")
                continue
            if len(seen_objs) > self.MAX_TREE_COMPONENTS:
                errors.append(f"tree exceeds {self.MAX_TREE_COMPONENTS} components (at '{path}')")
                return errors
            if id(node) in seen_objs:
                errors.append(
                    f"component '{node.get_id()}' ({_type_name_of(node)}) is reachable "
                    f"from multiple parents or is part of a cycle (at '{path}')"
                )
                continue
            seen_objs.add(id(node))
            errors.extend(node._validate())
            if node.get_id() in seen_ids:
                errors.append(
                    f"duplicate component id '{node.get_id()}': already used at "
                    f"'{seen_ids[node.get_id()]}', conflicts at '{path}'"
                )
            else:
                seen_ids[node.get_id()] = path
            if isinstance(node, UIContainer):
                for child in node._children:
                    if node._allowed_child_types and not isinstance(
                        child, node._allowed_child_types
                    ):
                        errors.append(
                            f"{_type_name_of(child)} '{child.get_id()}' is not an allowed "
                            f"child of {_type_name_of(node)} '{node.get_id()}'"
                        )
                    if child.get_parent() is not node:
                        errors.append(
                            f"component '{child.get_id()}' is in the children of "
                            f"'{node.get_id()}' but its parent pointer points at "
                            f"'{child.get_parent().get_id() if child.get_parent() else None}'"
                        )
                    queue.append((child, f"{path}/{child.get_id()}", depth + 1))
        return errors


class DashboardUIToolset:
    """The public namespace handed to initialize_dashboard_ui()."""

    Layout = Layout
    Page = Page
    Tab = Tab
    Section = Section
    Card = Card
    Row = Row
    RowAction = RowAction
    Column = Column
    Container = Container
    Text = Text
    Badge = Badge
    Metric = Metric
    Checkbox = Checkbox
    Toggle = Toggle
    TextInput = TextInput
    NumberInput = NumberInput
    Textarea = Textarea
    Select = Select
    RadioGroup = RadioGroup
    MultiSelect = MultiSelect
    ListView = ListView
    Table = Table
    Progress = Progress
    Button = Button
    Separator = Separator
    Description = Description
    DisplayText = DisplayText
    Header = Header
    Footer = Footer
    Grid = Grid
    Alert = Alert
    Toolbar = Toolbar
    Modal = Modal
    TileView = TileView
    BulkAction = BulkAction
    Form = Form
