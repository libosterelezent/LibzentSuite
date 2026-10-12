"""Dashboard UI API — builder core (Stage B).

Pure-core fluent builders for assembling dashboard UI trees, plus the
error hierarchy they raise. Later stages attach the registry, routing
and frontend; nothing here imports from adapters/, interfaces/ or
database/.
"""
from __future__ import annotations

from .builders import (
    Badge,
    Button,
    Card,
    Checkbox,
    Column,
    DashboardUIToolset,
    Form,
    Layout,
    ListView,
    Metric,
    MultiSelect,
    NumberInput,
    Page,
    Progress,
    RadioGroup,
    Row,
    Section,
    Select,
    Tab,
    Table,
    Text,
    TextInput,
    Textarea,
    Toggle,
    UIComponent,
    UIContainer,
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
    "DashboardUIToolset",
    "Layout",
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
    "UIComponent",
    "UIContainer",
    "UIError",
    "InvalidComponentIdError",
    "DuplicateComponentIdError",
    "InvalidChildError",
    "TreeValidationError",
    "FrozenLayoutError",
    "ActionRegistrationError",
]
