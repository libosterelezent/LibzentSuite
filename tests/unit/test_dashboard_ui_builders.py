"""Stage-B unit tests: dashboard UI builder core (core/dashboard_ui).

Pins the builder contract: automatic/explicit ids, fluent type-checked
setters, parent-child rules, parent-level + layout-level duplicate id
detection, cycle/multi-parent prevention, atomic multi-child adds, id
release on remove, JSON-safe serialization, tree validation and the
frozen (attached) state. Plain TestCase throughout — the builder core
is fully synchronous.
"""
from __future__ import annotations

import json
import unittest

from core.dashboard_ui import (
    ActionRegistrationError,
    Badge,
    Button,
    Card,
    Checkbox,
    Column,
    DashboardUIToolset,
    DuplicateComponentIdError,
    Form,
    FrozenLayoutError,
    InvalidChildError,
    InvalidComponentIdError,
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
    TreeValidationError,
    UIContainer,
    UIComponent,
    UIError,
)

ALL_COMPONENT_CLASSES = [
    Layout, Page, Tab, Section, Card, Row, Column, Text, Badge, Metric,
    Checkbox, Toggle, TextInput, NumberInput, Textarea, Select, RadioGroup,
    MultiSelect, ListView, Table, Progress, Button, Form,
]


def _action():
    return lambda: None


def _source():
    return lambda: []


def _setter_specs():
    """(class, [(setter, valid value), ...]) for the fluent-chaining test."""
    return [
        (Layout, [("set_title", "T"), ("set_check", _action())]),
        (Page, [("set_title", "T"), ("set_description", "D"),
                ("set_icon", "{icon}"), ("set_order", 2), ("set_check", _action())]),
        (Tab, [("set_title", "T"), ("set_icon", "star")]),
        (Section, [("set_title", "T"), ("set_description", "D")]),
        (Card, [("set_title", "T")]),
        (Row, []),
        (Column, []),
        (Text, [("set_value", "hi"), ("set_variant", "code")]),
        (Badge, [("set_value", "hot"), ("set_variant", "err")]),
        (Metric, [("set_label", "L"), ("set_value", "42"), ("set_sub", "S"),
                  ("set_level", "warn")]),
        (Checkbox, [("set_label", "L"), ("set_value", True),
                    ("set_description", "D"), ("set_action", _action()),
                    ("set_disabled", False)]),
        (Toggle, [("set_label", "L"), ("set_value", False),
                  ("set_description", "D"), ("set_action", _action())]),
        (TextInput, [("set_label", "L"), ("set_placeholder", "p"),
                     ("set_value", "v"), ("set_required", True),
                     ("set_disabled", False), ("set_description", "D"),
                     ("set_action", _action()), ("set_max_length", 10)]),
        (NumberInput, [("set_label", "L"), ("set_placeholder", "p"),
                       ("set_value", "7"), ("set_min_value", 0),
                       ("set_max_value", 10), ("set_step", 0.5),
                       ("set_action", _action())]),
        (Textarea, [("set_label", "L"), ("set_value", "v"),
                    ("set_rows", 4), ("set_action", _action())]),
        (Select, [("set_label", "L"), ("set_placeholder", "p"),
                  ("set_value", "a"), ("set_required", True),
                  ("set_disabled", False), ("set_options", ["a", "b"]),
                  ("set_action", _action())]),
        (RadioGroup, [("set_label", "L"), ("set_value", "a"),
                      ("set_options", ["a"]), ("set_action", _action())]),
        (MultiSelect, [("set_label", "L"), ("set_value", ["a", "b"]),
                       ("set_options", ["a"]), ("set_action", _action())]),
        (ListView, [("set_data_source", _source()), ("set_empty_text", "empty"),
                    ("set_action", _action()), ("set_disabled", False)]),
        (Table, [("set_columns", ["a", "b"]), ("set_data_source", _source()),
                 ("set_empty_text", "empty"), ("set_page_size", 5),
                 ("set_action", _action()), ("set_disabled", False)]),
        (Progress, [("set_value", 50.0), ("set_label", "L")]),
        (Button, [("set_label", "L"), ("set_description", "D"),
                  ("set_disabled", False), ("set_action", _action()),
                  ("set_variant", "danger"), ("set_confirm", "Sure?"),
                  ("set_loading", True)]),
        (Form, [("set_title", "T"), ("set_submit_action", _action()),
                ("set_submit_label", "Go")]),
    ]


def _assert_json_safe(test: unittest.TestCase, value, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            test.assertIsInstance(key, str, f"non-str key at {path}")
            _assert_json_safe(test, item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_json_safe(test, item, f"{path}[{index}]")
    elif isinstance(value, (str, int, float, bool)) or value is None:
        pass
    else:
        test.fail(f"non-JSON-safe {type(value).__name__} at {path}")


def _walk_dicts(node: dict):
    yield node
    for child in node.get("children", ()):
        yield from _walk_dicts(child)


class _RecursiveSection(Section):
    """Test-only container allowed to hold its own type so the cycle
    guard is reachable (the real type graph is a DAG by construction)."""

    _allowed_child_types = (Section,)


# ------------------------------------------------------------- 1. init
class EmptyInitializationTests(unittest.TestCase):
    def test_every_class_instantiates_empty(self) -> None:
        for cls in ALL_COMPONENT_CLASSES:
            with self.subTest(cls=cls.__name__):
                component = cls()
                self.assertIsInstance(component, UIComponent)
                self.assertRegex(component.get_id(), r"^[0-9a-f]{12}$")
                payload = component.to_dict()
                self.assertEqual(payload["id"], component.get_id())
                self.assertIsInstance(payload["type"], str)

    def test_container_type_names_are_distinct(self) -> None:
        names = [cls().to_dict()["type"] for cls in ALL_COMPONENT_CLASSES]
        self.assertEqual(len(names), len(set(names)))

    def test_kwargs_argument_is_accepted(self) -> None:
        # The base contract accepts **kwargs; unknown ones are ignored.
        Button(component_id="b1", future_flag=True)
        Section(component_id="s1", anything="goes")


# --------------------------------------------------- 2. generated ids
class GeneratedIdTests(unittest.TestCase):
    def test_format_is_uuid_hex_prefix(self) -> None:
        for cls in ALL_COMPONENT_CLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertRegex(cls().get_id(), r"^[0-9a-f]{12}$")

    def test_ids_are_unique_across_instances(self) -> None:
        ids = {Text().get_id() for _ in range(500)}
        self.assertEqual(len(ids), 500)

    def test_id_is_stable_for_lifetime(self) -> None:
        text = Text()
        first = text.get_id()
        text.set_value("x").set_variant("muted")
        self.assertEqual(text.get_id(), first)
        self.assertEqual(text.to_dict()["id"], first)
        self.assertEqual(text.to_dict()["id"], first)

    def test_generated_ids_are_json_safe_strings(self) -> None:
        for component_id in (Text().get_id() for _ in range(20)):
            self.assertIsInstance(json.dumps(component_id), str)


# --------------------------------------------------- 3. explicit ids
class ExplicitIdTests(unittest.TestCase):
    def test_valid_ids_accepted(self) -> None:
        for valid in ("a", "A-b_9", "9lives", "x" * 64, "-_", "Save_BTN-01"):
            with self.subTest(value=valid):
                self.assertEqual(Button(component_id=valid).get_id(), valid)
                self.assertEqual(Button().set_id(valid).get_id(), valid)

    def test_invalid_ids_raise_on_constructor(self) -> None:
        for invalid in ("a b", "a!", "café", "x" * 65, 42, ["x"]):
            with self.subTest(value=invalid):
                with self.assertRaises(InvalidComponentIdError):
                    Button(component_id=invalid)

    def test_falsy_constructor_id_means_generate(self) -> None:
        # The base contract: component_id="" / None -> auto-generate.
        for empty in ("", None):
            with self.subTest(value=empty):
                self.assertRegex(Button(component_id=empty).get_id(), r"^[0-9a-f]{12}$")

    def test_invalid_ids_raise_on_set_id(self) -> None:
        for invalid in ("", "a b", "a!", "café", "x" * 65, None, 42):
            with self.subTest(value=invalid):
                with self.assertRaises(InvalidComponentIdError):
                    Button().set_id(invalid)

    def test_failed_set_id_leaves_state_unchanged(self) -> None:
        button = Button(component_id="keep-me")
        with self.assertRaises(InvalidComponentIdError):
            button.set_id("not valid!")
        self.assertEqual(button.get_id(), "keep-me")

    def test_set_id_updates_parent_registry(self) -> None:
        section = Section()
        button = Button(component_id="old")
        section.add_component(button)
        button.set_id("new")
        self.assertNotIn("old", section._child_ids)
        self.assertIn("new", section._child_ids)
        # The released id can be reused by a new sibling...
        section.add_component(Button(component_id="old"))
        # ...and the new one now collides on rename.
        with self.assertRaises(DuplicateComponentIdError):
            button.set_id("old")
        self.assertEqual(button.get_id(), "new")


# ------------------------------------------------------ 4. chaining
class FluentChainingTests(unittest.TestCase):
    def test_every_setter_returns_self(self) -> None:
        for cls, setters in _setter_specs():
            component = cls()
            for name, value in setters:
                with self.subTest(cls=cls.__name__, setter=name):
                    self.assertIs(getattr(component, name)(value), component)

    def test_inherited_base_setters_return_self(self) -> None:
        for cls in (Text, Row, Metric, Progress):
            component = cls()
            self.assertIs(component.set_id(f"{cls.__name__}-x"), component)
            self.assertIs(component.set_title("T"), component)
            self.assertIs(component.set_check(_action()), component)
            self.assertIs(component.set_disabled(True), component)

    def test_container_methods_return_self(self) -> None:
        layout = Layout()
        page = Page()
        section = Section()
        self.assertIs(layout.add_page(page), layout)
        self.assertIs(page.add_section(section), page)
        self.assertIs(page.remove_section(section.get_id()), page)
        self.assertIs(layout.remove_page(page.get_id()), layout)
        card = Card()
        self.assertIs(section.add_component(card), section)
        self.assertIs(section.replace_child(card.get_id(), Badge()), section)
        self.assertIs(section.clear_children(), section)


# ------------------------------------------- 5. add / remove children
class ChildManagementTests(unittest.TestCase):
    def test_add_and_remove_single_children(self) -> None:
        layout = Layout()
        page = Page()
        layout.add_page(page)
        self.assertEqual(layout.pages(), [page])
        self.assertIs(page.get_parent(), layout)

        section, tab = Section(component_id="s1"), Tab(component_id="t1")
        page.add_section(section).add_tab(tab)
        self.assertEqual(page.sections(), [section])
        self.assertEqual(page.tabs(), [tab])
        self.assertEqual(page.children(), [section, tab])

        layout.remove_page(page.get_id())
        self.assertEqual(layout.pages(), [])
        self.assertIsNone(page.get_parent())

    def test_multi_add_preserves_insertion_order(self) -> None:
        page = Page()
        first, second, third = (Section(component_id=n) for n in "abc")
        page.add_sections(first, second, third)
        self.assertEqual(page.sections(), [first, second, third])

    def test_typed_remove_rejects_wrong_category(self) -> None:
        page = Page()
        tab = Tab(component_id="t1")
        page.add_tab(tab)
        with self.assertRaises(InvalidChildError):
            page.remove_section("t1")
        self.assertEqual(page.tabs(), [tab])

    def test_remove_unknown_id_raises(self) -> None:
        with self.assertRaises(InvalidChildError):
            Layout().remove_page("nope")
        with self.assertRaises(InvalidChildError):
            Section().remove_component("nope")

    def test_get_child(self) -> None:
        section = Section()
        button = Button(component_id="b1")
        section.add_component(button)
        self.assertIs(section.get_child("b1"), button)
        with self.assertRaises(InvalidChildError):
            section.get_child("missing")

    def test_clear_children_detaches_all(self) -> None:
        section = Section()
        a, b = Text(component_id="a"), Badge(component_id="b")
        section.add_components(a, b)
        section.clear_children()
        self.assertEqual(section.children(), [])
        self.assertEqual(section._child_ids, set())
        self.assertIsNone(a.get_parent())
        self.assertIsNone(b.get_parent())

    def test_replace_child(self) -> None:
        section = Section()
        old = Text(component_id="old")
        section.add_component(old)
        replacement = Badge(component_id="new")
        section.replace_child("old", replacement)
        self.assertEqual(section.children(), [replacement])
        self.assertIsNone(old.get_parent())
        self.assertIs(replacement.get_parent(), section)
        self.assertNotIn("old", section._child_ids)

    def test_replace_child_with_same_id(self) -> None:
        section = Section()
        section.add_component(Text(component_id="slot"))
        section.replace_child("slot", Badge(component_id="slot"))
        self.assertEqual(section.children()[0].to_dict()["type"], "badge")

    def test_replace_child_unknown_id_raises(self) -> None:
        with self.assertRaises(InvalidChildError):
            Section().replace_child("ghost", Badge())

    def test_form_field_management(self) -> None:
        form = Form()
        name, count = TextInput(component_id="name"), NumberInput(component_id="count")
        form.add_fields(name, count)
        self.assertEqual(form.fields(), [name, count])
        form.remove_field("name")
        self.assertEqual(form.fields(), [count])
        self.assertIsNone(name.get_parent())

    def test_tab_adds_sections(self) -> None:
        tab = Tab()
        section = Section()
        tab.add_sections(section)
        self.assertEqual(tab.children(), [section])


# ------------------------------------------------- 6. atomicity
class AtomicAddTests(unittest.TestCase):
    def test_bad_type_in_batch_adds_nothing(self) -> None:
        section = Section()
        good_text, good_badge = Text(component_id="a"), Badge(component_id="b")
        with self.assertRaises(InvalidChildError):
            section.add_components(good_text, good_badge, Page(component_id="p"))
        self.assertEqual(section.children(), [])
        self.assertIsNone(good_text.get_parent())
        self.assertIsNone(good_badge.get_parent())
        self.assertEqual(section._child_ids, set())

    def test_duplicate_inside_batch_adds_nothing(self) -> None:
        page = Page()
        with self.assertRaises(DuplicateComponentIdError):
            page.add_sections(Section(component_id="dup"), Section(component_id="dup"))
        self.assertEqual(page.children(), [])

    def test_duplicate_against_existing_adds_nothing(self) -> None:
        section = Section()
        existing = Text(component_id="taken")
        section.add_component(existing)
        fresh = Badge(component_id="fresh")
        with self.assertRaises(DuplicateComponentIdError):
            section.add_components(fresh, Metric(component_id="taken"))
        self.assertEqual(section.children(), [existing])
        self.assertIsNone(fresh.get_parent())

    def test_failed_batch_leaves_no_id_residue(self) -> None:
        section = Section()
        with self.assertRaises(InvalidChildError):
            section.add_components(Text(component_id="x"), Layout())
        # "x" was never registered, so it is still available.
        section.add_component(Text(component_id="x"))
        self.assertEqual(len(section.children()), 1)

    def test_add_pages_is_atomic(self) -> None:
        layout = Layout()
        with self.assertRaises(InvalidChildError):
            layout.add_pages(Page(component_id="p1"), Section(component_id="p2"))
        self.assertEqual(layout.pages(), [])


# ------------------------------------------------ 7. parent-child rules
class InvalidChildTests(unittest.TestCase):
    def test_button_under_page(self) -> None:
        with self.assertRaises(InvalidChildError):
            Page().add_section(Button(component_id="b"))

    def test_layout_only_accepts_pages(self) -> None:
        for bad in (Section(), Button(), Text(), Tab()):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(InvalidChildError):
                    Layout().add_page(bad)

    def test_page_only_accepts_sections_and_tabs(self) -> None:
        for bad in (Button(), Card(), Layout(), Text()):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(InvalidChildError):
                    Page().add_section(bad)

    def test_tab_only_accepts_sections(self) -> None:
        with self.assertRaises(InvalidChildError):
            Tab().add_section(Card())

    def test_no_nested_rows(self) -> None:
        with self.assertRaises(InvalidChildError):
            Row().add_component(Row())
        with self.assertRaises(InvalidChildError):
            Column().add_component(Row())

    def test_column_inside_row_is_allowed(self) -> None:
        row = Row()
        column = Column()
        row.add_component(column)
        self.assertIs(column.get_parent(), row)

    def test_no_section_inside_section_or_card(self) -> None:
        with self.assertRaises(InvalidChildError):
            Section().add_component(Section())
        with self.assertRaises(InvalidChildError):
            Card().add_component(Section())

    def test_form_only_accepts_fields(self) -> None:
        for good in (TextInput(), NumberInput(), Textarea(), Select(),
                     RadioGroup(), MultiSelect(), Checkbox(), Toggle()):
            Form().add_field(good)
        for bad in (Button(), Text(), Section(), Card()):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(InvalidChildError):
                    Form().add_field(bad)

    def test_non_component_rejected(self) -> None:
        for bad in ("button", 42, None, object()):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidChildError):
                    Section().add_component(bad)

    def test_layout_rejects_non_page_via_multi_add(self) -> None:
        with self.assertRaises(InvalidChildError):
            Layout().add_pages(Page(), "not-a-page")


# --------------------------------------------------- 8. duplicates
class DuplicateIdTests(unittest.TestCase):
    def test_sibling_duplicate_on_add(self) -> None:
        section = Section(component_id="holder")
        section.add_component(Button(component_id="fire"))
        with self.assertRaises(DuplicateComponentIdError) as ctx:
            section.add_component(Badge(component_id="fire"))
        error = ctx.exception
        self.assertEqual(error.component_id, "fire")
        self.assertEqual(error.existing_type, "Button")
        self.assertEqual(error.conflicting_type, "Badge")
        self.assertEqual(error.existing_path, "holder/fire")
        self.assertEqual(error.conflicting_path, "fire")
        self.assertIn("fire", str(error))

    def test_duplicate_error_carries_paths(self) -> None:
        layout = Layout(component_id="root")
        page = Page(component_id="page-1")
        section = Section(component_id="sec")
        layout.add_page(page)
        page.add_section(section)
        section.add_component(Text(component_id="body"))
        with self.assertRaises(DuplicateComponentIdError) as ctx:
            section.add_component(Badge(component_id="body"))
        self.assertEqual(ctx.exception.existing_path, "root/page-1/sec/body")
        self.assertEqual(ctx.exception.conflicting_path, "body")

    def test_nested_same_id_caught_by_tree_validation(self) -> None:
        # Parent-level checks only see siblings; a child sharing its
        # parent's id is caught by Layout.validate().
        layout = Layout().add_page(Page(component_id="p").add_section(
            Section(component_id="s").add_component(Card(component_id="s"))))
        errors = layout.validate()
        self.assertTrue(any("duplicate component id 's'" in e for e in errors), errors)

    def test_cross_page_duplicate_caught_by_tree_validation(self) -> None:
        layout = Layout(component_id="root")
        layout.add_pages(
            Page(component_id="p1").add_section(
                Section(component_id="s1").add_component(Button(component_id="go"))),
            Page(component_id="p2").add_section(
                Section(component_id="s2").add_component(Button(component_id="go"))),
        )
        errors = layout.validate()
        self.assertTrue(any("duplicate component id 'go'" in e for e in errors), errors)
        self.assertTrue(any("root/p1/s1/go" in e and "root/p2/s2/go" in e
                            for e in errors), errors)

    def test_set_id_to_sibling_id_raises(self) -> None:
        section = Section()
        first = Button(component_id="one")
        second = Badge(component_id="two")
        section.add_components(first, second)
        with self.assertRaises(DuplicateComponentIdError) as ctx:
            second.set_id("one")
        self.assertEqual(second.get_id(), "two")  # unchanged
        self.assertIn("one", section._child_ids)

    def test_valid_tree_has_no_duplicate_errors(self) -> None:
        layout = Layout().add_pages(
            Page(component_id="p1").add_section(
                Section(component_id="s1").add_component(Button(component_id="go-1"))),
            Page(component_id="p2").add_section(
                Section(component_id="s2").add_component(Button(component_id="go-2"))),
        )
        self.assertEqual(layout.validate(), [])


# ------------------------------------------------------- 9. cycles
class CycleTests(unittest.TestCase):
    def test_component_cannot_contain_itself(self) -> None:
        outer = _RecursiveSection(component_id="outer")
        with self.assertRaises(TreeValidationError):
            outer.add_component(outer)

    def test_component_cannot_add_its_ancestor(self) -> None:
        outer = _RecursiveSection(component_id="outer")
        inner = _RecursiveSection(component_id="inner")
        outer.add_component(inner)
        with self.assertRaises(TreeValidationError):
            inner.add_component(outer)
        self.assertEqual(inner.children(), [])

    def test_deep_ancestor_cycle_blocked(self) -> None:
        a = _RecursiveSection(component_id="a")
        b = _RecursiveSection(component_id="b")
        c = _RecursiveSection(component_id="c")
        a.add_component(b)
        b.add_component(c)
        with self.assertRaises(TreeValidationError):
            c.add_component(a)


# -------------------------------------------------- 10. multi-parent
class MultipleParentTests(unittest.TestCase):
    def test_component_with_parent_rejected_elsewhere(self) -> None:
        first, second = Section(component_id="s1"), Section(component_id="s2")
        button = Button(component_id="b")
        first.add_component(button)
        with self.assertRaises(InvalidChildError):
            second.add_component(button)
        self.assertIs(button.get_parent(), first)
        self.assertEqual(second.children(), [])

    def test_readd_to_same_parent_rejected(self) -> None:
        section = Section()
        button = Button(component_id="b")
        section.add_component(button)
        with self.assertRaises(InvalidChildError):
            section.add_component(button)
        self.assertEqual(section.children(), [button])


# ------------------------------------------------- 11. id release
class IdReleaseTests(unittest.TestCase):
    def test_removed_id_can_be_reused(self) -> None:
        section = Section()
        section.add_component(Button(component_id="worker"))
        section.remove_component("worker")
        section.add_component(Badge(component_id="worker"))
        self.assertEqual(type(section.get_child("worker")).__name__, "Badge")

    def test_removed_page_id_can_be_reused(self) -> None:
        layout = Layout()
        layout.add_page(Page(component_id="home"))
        layout.remove_page("home")
        layout.add_page(Page(component_id="home"))
        self.assertEqual(len(layout.pages()), 1)

    def test_removed_child_is_fully_detached(self) -> None:
        page = Page()
        section = Section(component_id="s")
        page.add_section(section)
        page.remove_section("s")
        self.assertIsNone(section.get_parent())
        # The detached section is free to join another tree.
        other = Page()
        other.add_section(section)
        self.assertIs(section.get_parent(), other)


# ------------------------------------------------ 12. serialization
class SerializationTests(unittest.TestCase):
    def _full_tree(self) -> Layout:
        return (
            Layout(component_id="root")
            .add_page(
                Page(component_id="home").set_title("Home").set_icon("{i}").set_order(1)
                .add_section(
                    Section(component_id="status").set_title("Status").set_description("d")
                    .add_component(Metric(component_id="m1").set_label("Accounts")
                                   .set_value("12").set_level("ok"))
                    .add_component(Text(component_id="t1").set_value("hello").set_variant("muted"))
                    .add_component(Badge(component_id="b1").set_value("live").set_variant("ok"))
                    .add_component(Button(component_id="sync").set_label("Sync")
                                   .set_variant("primary").set_action(_action())
                                   .set_confirm("Sure?").set_loading(True))
                    .add_component(Progress(component_id="pg").set_value(42.5).set_label("Syncing"))
                )
                .add_section(
                    Section(component_id="grid")
                    .add_component(Row(component_id="r1").add_component(
                        Column(component_id="c1").add_component(Text(component_id="cell"))))
                )
                .add_tab(Tab(component_id="tab1").set_title("Advanced").set_icon("*")
                         .add_section(Section(component_id="ts1")))
            )
            .add_page(
                Page(component_id="data")
                .add_section(
                    Section(component_id="ds1")
                    .add_component(ListView(component_id="lv").set_data_source(_source())
                                   .set_empty_text("nothing").set_action(_action()))
                    .add_component(Table(component_id="tbl").set_columns(["a", "b"])
                                   .set_data_source(_source()).set_page_size(5))
                    .add_component(
                        Form(component_id="f1").set_title("Add").set_submit_action(_action())
                        .set_submit_label("Go")
                        .add_field(TextInput(component_id="ti1").set_label("Name")
                                   .set_placeholder("...").set_required(True).set_max_length(32))
                        .add_field(NumberInput(component_id="ni1").set_min_value(0)
                                   .set_max_value(10).set_step(1))
                        .add_field(Textarea(component_id="ta1").set_rows(4))
                        .add_field(Checkbox(component_id="ck1").set_label("OK").set_value(True))
                        .add_field(Toggle(component_id="tg1").set_label("Auto"))
                        .add_field(Select(component_id="sel1")
                                   .set_options(["a", {"value": "b", "label": "Bee"}])
                                   .set_action(_action()))
                        .add_field(RadioGroup(component_id="rg1").set_options(["x"]))
                        .add_field(MultiSelect(component_id="ms1").set_value(["a"]).set_disabled(True)))
                )
            )
        )

    def test_to_dict_is_json_serializable(self) -> None:
        layout = self._full_tree()
        payload = layout.to_dict()
        _assert_json_safe(self, payload)
        json.dumps(payload)  # must not raise on callables

    def test_structure_shape(self) -> None:
        payload = self._full_tree().to_dict()
        self.assertEqual(payload["type"], "layout")
        self.assertEqual(payload["id"], "root")
        self.assertEqual([p["id"] for p in payload["children"]], ["home", "data"])
        home = payload["children"][0]
        self.assertEqual(home["type"], "page")
        self.assertEqual(home["title"], "Home")
        self.assertEqual(home["order"], 1)
        self.assertEqual([c["id"] for c in home["children"]], ["status", "grid", "tab1"])
        button = home["children"][0]["children"][3]
        self.assertEqual(button, {"type": "button", "id": "sync", "action": True,
                                  "label": "Sync", "variant": "primary",
                                  "confirm": "Sure?", "loading": True})

    def test_no_parent_or_internal_state_leaks(self) -> None:
        layout = self._full_tree().set_check(_action())
        for node in _walk_dicts(layout.to_dict()):
            for banned in ("parent", "_parent", "_check", "check", "_children",
                           "_child_ids", "_frozen", "_action", "data_source"):
                self.assertNotIn(banned, node)

    def test_actions_serialize_as_flag_only(self) -> None:
        layout = self._full_tree()
        button = layout.pages()[0].sections()[0].children()[3]
        self.assertEqual(button.to_dict()["action"], True)
        form = layout.pages()[1].sections()[0].children()[2]
        self.assertEqual(form.to_dict()["submit_action"], True)
        plain = Button(component_id="plain")
        self.assertNotIn("action", plain.to_dict())

    def test_options_are_normalized(self) -> None:
        select = Select().set_options(["a", {"value": "b", "label": "Bee"}])
        self.assertEqual(select.to_dict()["options"],
                         [{"value": "a", "label": "a"},
                          {"value": "b", "label": "Bee"}])

    def test_to_dict_is_stable_and_not_live(self) -> None:
        section = Section(component_id="s")
        section.add_component(Text(component_id="t"))
        first = section.to_dict()
        second = section.to_dict()
        self.assertEqual(first, second)
        first["children"][0]["id"] = "mutated"
        self.assertEqual(section.to_dict()["children"][0]["id"], "t")

    def test_leaf_defaults_stay_minimal(self) -> None:
        payload = Text().to_dict()
        self.assertEqual(set(payload), {"type", "id"})
        self.assertEqual(payload["type"], "text")
        self.assertEqual(set(Button().to_dict()), {"type", "id"})


# ------------------------------------------------ 13. tree validation
class TreeValidationTests(unittest.TestCase):
    def test_valid_tree_returns_empty_list(self) -> None:
        layout = Layout(component_id="root")
        layout.add_pages(
            Page(component_id="p1").add_sections(
                Section(component_id="s1").add_components(
                    Button(component_id="go").set_action(_action()),
                    Text(component_id="note")),
                Section(component_id="s2").add_component(
                    Card(component_id="c1").add_component(Badge(component_id="ok")))),
            Page(component_id="p2").add_tab(
                Tab(component_id="t1").add_section(Section(component_id="s3"))),
        )
        self.assertEqual(layout.validate(), [])

    def test_wrong_child_type_reported(self) -> None:
        page = Page(component_id="p")
        page._add_child(Button(component_id="intruder"))  # bypass checks on purpose
        layout = Layout().add_page(page)
        errors = layout.validate()
        self.assertTrue(any("not an allowed child" in e and "intruder" in e
                            for e in errors), errors)

    def test_mislinked_parent_reported(self) -> None:
        section = Section(component_id="s")
        button = Button(component_id="b")
        section._add_child(button)
        button._detach()  # orphaned pointer while still listed
        layout = Layout().add_page(Page(component_id="p").add_section(section))
        errors = layout.validate()
        self.assertTrue(any("parent pointer" in e for e in errors), errors)

    def test_shared_child_reported(self) -> None:
        button = Button(component_id="shared")
        section_a, section_b = Section(component_id="a"), Section(component_id="b")
        section_a._add_child(button)
        section_b._add_child(button)  # bypass: same object under two parents
        layout = Layout().add_page(Page(component_id="p")
                                   .add_section(section_a).add_section(section_b))
        errors = layout.validate()
        self.assertTrue(any("multiple parents" in e or "cycle" in e for e in errors), errors)

    def test_broken_action_reported(self) -> None:
        button = Button(component_id="b").set_action(_action())
        button._action = "not-callable"  # simulate corruption
        layout = Layout().add_page(Page(component_id="p").add_section(
            Section(component_id="s").add_component(button)))
        errors = layout.validate()
        self.assertTrue(any("non-callable action" in e for e in errors), errors)

    def test_bad_id_reported(self) -> None:
        button = Button(component_id="ok")
        button._id = "not valid!"  # simulate corruption
        layout = Layout().add_page(Page(component_id="p").add_section(
            Section(component_id="s").add_component(button)))
        errors = layout.validate()
        self.assertTrue(any("component id" in e for e in errors), errors)


# --------------------------------------------- 14. setter validation
class SetterValidationTests(unittest.TestCase):
    def test_bad_types_raise(self) -> None:
        cases = [
            (Button(), "set_disabled", "yes", UIError),
            (Page(), "set_order", "1", UIError),
            (Page(), "set_order", 1.5, UIError),
            (Page(), "set_order", True, UIError),
            (NumberInput(), "set_min_value", "5", UIError),
            (NumberInput(), "set_max_value", None, UIError),
            (NumberInput(), "set_step", "1", UIError),
            (Button(), "set_action", "nope", ActionRegistrationError),
            (Form(), "set_submit_action", 42, ActionRegistrationError),
            (Text(), "set_check", "nope", UIError),
            (Select(), "set_options", "nope", UIError),
            (Select(), "set_options", [1, 2], UIError),
            (Select(), "set_options", [{"value": "a"}], UIError),
            (Select(), "set_options", [{"label": "a"}], UIError),
            (Checkbox(), "set_value", "yes", UIError),
            (Toggle(), "set_value", 1, UIError),
            (Progress(), "set_value", 101, UIError),
            (Progress(), "set_value", -0.5, UIError),
            (Progress(), "set_value", "50", UIError),
            (TextInput(), "set_max_length", "10", UIError),
            (TextInput(), "set_required", 1, UIError),
            (Textarea(), "set_rows", "4", UIError),
            (Table(), "set_page_size", 2.5, UIError),
            (Table(), "set_columns", "cols", UIError),
            (Table(), "set_columns", ["a", 2], UIError),
            (Table(), "set_data_source", "fetch", UIError),
            (ListView(), "set_data_source", 42, UIError),
            (MultiSelect(), "set_value", "a", UIError),
            (Button(), "set_variant", "sparkly", UIError),
            (Button(), "set_loading", "yes", UIError),
            (Button(), "set_label", 5, UIError),
            (Text(), "set_variant", "loud", UIError),
            (Badge(), "set_variant", "critical", UIError),
            (Metric(), "set_level", "critical", UIError),
            (Page(), "set_title", 5, UIError),
            (Page(), "set_description", None, UIError),
        ]
        for component, setter, value, expected in cases:
            with self.subTest(cls=type(component).__name__, setter=setter, value=value):
                with self.assertRaises(expected):
                    getattr(component, setter)(value)

    def test_valid_boundaries_accepted(self) -> None:
        Progress().set_value(0)
        Progress().set_value(100)
        Progress().set_value(50)  # int accepted where float expected
        NumberInput().set_min_value(0).set_max_value(9.5).set_step(1)
        Metric().set_level("")
        Page().set_order(0)

    def test_failed_setter_does_not_corrupt_state(self) -> None:
        button = Button(component_id="b").set_label("Keep")
        with self.assertRaises(UIError):
            button.set_label(9)
        self.assertEqual(button.to_dict()["label"], "Keep")

        select = Select().set_options(["a"])
        with self.assertRaises(UIError):
            select.set_options([3])
        self.assertEqual(select.to_dict()["options"], [{"value": "a", "label": "a"}])

        progress = Progress().set_value(25)
        with self.assertRaises(UIError):
            progress.set_value(500)
        self.assertEqual(progress.to_dict()["value"], 25)

        number = NumberInput().set_min_value(2)
        with self.assertRaises(UIError):
            number.set_min_value("x")
        self.assertEqual(number.to_dict()["min_value"], 2)


# ------------------------------------------------------ 15. frozen
class FrozenStateTests(unittest.TestCase):
    def _frozen_tree(self) -> Layout:
        page = Page(component_id="p")
        section = Section(component_id="s")
        text = Text(component_id="t")
        layout = Layout(component_id="root").add_page(page)
        page.add_section(section)
        section.add_component(text)
        layout._freeze()
        return layout

    def test_freeze_cascades_to_subtree(self) -> None:
        layout = self._frozen_tree()
        page = layout.pages()[0]
        self.assertTrue(layout._frozen)
        self.assertTrue(page._frozen)
        self.assertTrue(page.sections()[0]._frozen)
        self.assertTrue(page.sections()[0].children()[0]._frozen)

    def test_mutation_before_freeze_is_allowed(self) -> None:
        layout = Layout().add_page(Page(component_id="p"))
        layout.pages()[0].set_title("fine")
        self.assertEqual(layout.pages()[0].to_dict()["title"], "fine")

    def test_setters_raise_when_frozen(self) -> None:
        layout = self._frozen_tree()
        text = layout.pages()[0].sections()[0].children()[0]
        for setter, value in (("set_value", "x"), ("set_variant", "code"),
                              ("set_id", "new-id"), ("set_title", "T"),
                              ("set_disabled", True), ("set_check", _action())):
            with self.subTest(setter=setter):
                with self.assertRaises(FrozenLayoutError):
                    getattr(text, setter)(value)
        self.assertEqual(text.get_id(), "t")

    def test_container_ops_raise_when_frozen(self) -> None:
        layout = self._frozen_tree()
        page = layout.pages()[0]
        section = page.sections()[0]
        with self.assertRaises(FrozenLayoutError):
            layout.add_page(Page())
        with self.assertRaises(FrozenLayoutError):
            layout.remove_page("p")
        with self.assertRaises(FrozenLayoutError):
            page.add_section(Section())
        with self.assertRaises(FrozenLayoutError):
            page.remove_section("s")
        with self.assertRaises(FrozenLayoutError):
            section.add_component(Text())
        with self.assertRaises(FrozenLayoutError):
            section.remove_component("t")
        with self.assertRaises(FrozenLayoutError):
            section.clear_children()
        with self.assertRaises(FrozenLayoutError):
            section.replace_child("t", Badge())
        self.assertEqual(len(section.children()), 1)

    def test_action_registration_blocked_when_frozen(self) -> None:
        button = Button(component_id="b")
        layout = Layout(component_id="root").add_page(
            Page(component_id="p").add_section(
                Section(component_id="s").add_component(button)))
        layout._freeze()
        with self.assertRaises(FrozenLayoutError):
            button.set_action(_action())
        self.assertNotIn("action", button.to_dict())
        # An unfrozen form elsewhere is still fully mutable.
        form = Form(component_id="f").set_submit_action(_action())
        self.assertEqual(form.to_dict()["submit_action"], True)


# ------------------------------------------------- errors & toolset
class ErrorHierarchyTests(unittest.TestCase):
    def test_all_errors_subclass_ui_error(self) -> None:
        for error in (InvalidComponentIdError, DuplicateComponentIdError,
                      InvalidChildError, TreeValidationError, FrozenLayoutError,
                      ActionRegistrationError):
            with self.subTest(error=error.__name__):
                self.assertTrue(issubclass(error, UIError))
        self.assertTrue(issubclass(UIError, ValueError))

    def test_errors_are_catchable_as_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            Button(component_id="bad id")


class ToolsetTests(unittest.TestCase):
    def test_toolset_exposes_every_builder(self) -> None:
        expected = {
            "Layout": Layout, "Page": Page, "Tab": Tab, "Section": Section,
            "Card": Card, "Row": Row, "Column": Column, "Text": Text,
            "Badge": Badge, "Metric": Metric, "Checkbox": Checkbox,
            "Toggle": Toggle, "TextInput": TextInput, "NumberInput": NumberInput,
            "Textarea": Textarea, "Select": Select, "RadioGroup": RadioGroup,
            "MultiSelect": MultiSelect, "ListView": ListView, "Table": Table,
            "Progress": Progress, "Button": Button, "Form": Form,
        }
        for name, cls in expected.items():
            with self.subTest(name=name):
                self.assertIs(getattr(DashboardUIToolset, name), cls)

    def test_toolset_builds_a_working_tree(self) -> None:
        ui = DashboardUIToolset
        layout = ui.Layout().add_page(
            ui.Page().add_section(
                ui.Section().add_component(
                    ui.Button().set_label("Go").set_action(_action()))))
        self.assertEqual(layout.validate(), [])
        self.assertEqual(layout.to_dict()["children"][0]["children"][0]
                         ["children"][0]["type"], "button")


class BaseContractTests(unittest.TestCase):
    def test_getters(self) -> None:
        check = _action()
        button = Button(component_id="b").set_check(check)
        self.assertEqual(button.get_id(), "b")
        self.assertIs(button.get_check(), check)
        self.assertIsNone(button.get_parent())

    def test_uicontainer_is_uicomponent(self) -> None:
        self.assertTrue(issubclass(UIContainer, UIComponent))
        self.assertTrue(issubclass(Section, UIContainer))


if __name__ == "__main__":
    unittest.main()


class TestWidthHeight(unittest.TestCase):
    """set_width/set_height on every component + CSS-size validation."""

    def test_number_becomes_px(self):
        c = Button().set_width(300)
        self.assertEqual(c.to_dict()["width"], "300px")
        c2 = Card().set_height(200)
        self.assertEqual(c2.to_dict()["height"], "200px")

    def test_css_string_passthrough(self):
        for size in ("100%", "50vh", "12rem", "300px", "50vw", "2fr"):
            c = Section().set_width(size)
            self.assertEqual(c.to_dict()["width"], size)

    def test_float_px(self):
        c = Metric().set_width(150.5)
        self.assertEqual(c.to_dict()["width"], "150.5px")

    def test_invalid_rejected(self):
        for bad in (-100, 0, "100xp", "abc", True, None, ""):
            with self.assertRaises(UIError):
                Button().set_width(bad)
            with self.assertRaises(UIError):
                Button().set_height(bad)

    def test_unset_not_serialized(self):
        c = Button()
        self.assertNotIn("width", c.to_dict())
        self.assertNotIn("height", c.to_dict())

    def test_fluent_chaining(self):
        c = Card().set_width("50%").set_height("300px").set_title("T")
        d = c.to_dict()
        self.assertEqual(d["width"], "50%")
        self.assertEqual(d["height"], "300px")
        self.assertEqual(d["title"], "T")

    def test_frozen_rejects_size_change(self):
        c = Button().set_width("100%")
        c._freeze()
        with self.assertRaises(FrozenLayoutError):
            c.set_width("50%")

    def test_available_on_all_components(self):
        from core.dashboard_ui import DashboardUIToolset
        ui = DashboardUIToolset()
        for name in ("Layout", "Page", "Tab", "Section", "Card", "Row",
                     "Column", "Text", "Badge", "Metric", "Checkbox",
                     "Toggle", "TextInput", "NumberInput", "Textarea",
                     "Select", "RadioGroup", "MultiSelect", "ListView",
                     "Table", "Progress", "Button", "Form"):
            cls = getattr(ui, name)
            inst = cls()
            self.assertTrue(hasattr(inst, "set_width"), f"{name} missing set_width")
            self.assertTrue(hasattr(inst, "set_height"), f"{name} missing set_height")


class TestContainerComponent(unittest.TestCase):
    """The neutral grouping box: direction, gap, alignment, nesting."""

    def _ui(self):
        from core.dashboard_ui import DashboardUIToolset
        return DashboardUIToolset()

    def test_defaults_serialize_minimal(self):
        c = self._ui().Container(component_id="c")
        self.assertEqual(c._props(), {})   # no chrome, no forced props

    def test_direction_gap_align_justify_serialize(self):
        c = (self._ui().Container(component_id="c")
             .set_direction("horizontal").set_gap("12px")
             .set_align("center").set_justify("between"))
        self.assertEqual(c._props(), {"direction": "horizontal",
                                      "gap": "12px",
                                      "align": "center",
                                      "justify": "between"})

    def test_gap_accepts_number_as_pixels(self):
        c = self._ui().Container(component_id="c").set_gap(8)
        self.assertEqual(c._props()["gap"], "8px")

    def test_direction_rejects_unknown(self):
        from core.dashboard_ui.errors import UIError
        with self.assertRaises(UIError):
            self._ui().Container(component_id="c").set_direction("diagonal")

    def test_holds_content_and_nests_containers(self):
        ui = self._ui()
        inner = ui.Container(component_id="inner").add_component(
            ui.DisplayText(component_id="dt").set_value("x"))
        outer = ui.Container(component_id="outer").add_component(inner)
        self.assertEqual(outer._children[0].get_id(), "inner")

    def test_container_in_section_and_row(self):
        ui = self._ui()
        ui.Section(component_id="s").add_component(
            ui.Container(component_id="c1"))
        ui.Row(component_id="r").add_component(
            ui.Container(component_id="c2"))

    def test_layout_validates_in_a_full_tree(self):
        from core.dashboard_ui.registry import DashboardUIRegistry
        ui = self._ui()
        page = (ui.Page(component_id="p")
                .add_section(ui.Section(component_id="s").add_component(
                    ui.Container(component_id="c")
                    .set_direction("horizontal")
                    .add_component(ui.DisplayText(component_id="d")
                                   .set_value("hi")))))
        registry = DashboardUIRegistry()
        registry.attach("t", ui.Layout(component_id="l").add_page(page))
        snapshot = registry.get_layout("t")
        self.assertEqual(snapshot["children"][0]["children"][0]
                         ["children"][0]["type"], "container")


class TestDisplayTextComponent(unittest.TestCase):
    """Read-only presentation text: tone × size × weight × alignment."""

    def _ui(self):
        from core.dashboard_ui import DashboardUIToolset
        return DashboardUIToolset()

    def test_full_presentation_serialize(self):
        d = (self._ui().DisplayText(component_id="d")
             .set_value("99.2%").set_tone("ok").set_size("xl")
             .set_weight("bold").set_align("right").set_monospace(True))
        self.assertEqual(d._props(), {
            "value": "99.2%", "tone": "ok", "size": "xl",
            "weight": "bold", "align": "right", "monospace": True})

    def test_invalid_everything_rejected(self):
        from core.dashboard_ui.errors import UIError
        dt = self._ui().DisplayText(component_id="d")
        for bad in (lambda: dt.set_tone("purple"),
                    lambda: dt.set_size("huge"),
                    lambda: dt.set_weight("chunky"),
                    lambda: dt.set_align("justified")):
            with self.assertRaises(UIError):
                bad()

    def test_value_must_be_string(self):
        from core.dashboard_ui.errors import UIError
        with self.assertRaises(UIError):
            self._ui().DisplayText(component_id="d").set_value(42)

    def test_participates_in_alignment(self):
        d = (self._ui().DisplayText(component_id="d").set_value("v")
             .set_grow(1).set_align_self("center"))
        props = d._props()
        self.assertEqual(props["grow"], 1)
        self.assertEqual(props["align_self"], "center")


class TestAlignmentVocabulary(unittest.TestCase):
    """The two-axis flex vocabulary shared by every component."""

    def _ui(self):
        from core.dashboard_ui import DashboardUIToolset
        return DashboardUIToolset()

    def test_grow_shrink_bounds(self):
        from core.dashboard_ui.errors import UIError
        b = self._ui().Button(component_id="b")
        for bad in (-1, 101, 1.5):
            with self.assertRaises(UIError):
                b.set_grow(bad)
        b.set_grow(0)
        b.set_grow(100)
        b.set_shrink(0)

    def test_align_self_choices(self):
        from core.dashboard_ui.errors import UIError
        b = self._ui().Button(component_id="b")
        for good in ("auto", "start", "center", "end", "stretch", "baseline"):
            b.set_align_self(good)
        with self.assertRaises(UIError):
            b.set_align_self("middle")

    def test_row_column_align_justify(self):
        ui = self._ui()
        r = ui.Row(component_id="r").set_align("baseline").set_justify("evenly")
        self.assertEqual((r._props()["align"], r._props()["justify"]),
                         ("baseline", "evenly"))
        c = ui.Column(component_id="c").set_align("stretch").set_justify("center")
        self.assertEqual((c._props()["align"], c._props()["justify"]),
                         ("stretch", "center"))
        with self.assertRaises(Exception):
            ui.Row(component_id="r2").set_justify("spaced")

    def test_section_and_card_carry_alignment_too(self):
        ui = self._ui()
        s = ui.Section(component_id="s").set_align("center")
        card = ui.Card(component_id="cd").set_justify("between")
        self.assertEqual(s._props()["align"], "center")
        self.assertEqual(card._props()["justify"], "between")

    def test_alignment_available_on_every_component(self):
        ui = self._ui()
        for name in ("Layout", "Page", "Tab", "Section", "Card", "Row",
                     "Column", "Container", "Text", "DisplayText", "Badge",
                     "Metric", "Checkbox", "Toggle", "TextInput",
                     "NumberInput", "Textarea", "Select", "RadioGroup",
                     "MultiSelect", "ListView", "Table", "Progress",
                     "Button", "Form", "Separator", "Description",
                     "Header", "Footer"):
            cls = getattr(ui, name)
            inst = cls()
            self.assertTrue(hasattr(inst, "set_grow"), f"{name} missing set_grow")
            self.assertTrue(hasattr(inst, "set_shrink"), f"{name} missing set_shrink")
            self.assertTrue(hasattr(inst, "set_align_self"), f"{name} missing set_align_self")

    def test_side_by_side_ratio_tree_validates(self):
        from core.dashboard_ui.registry import DashboardUIRegistry
        ui = self._ui()
        row = (ui.Row(component_id="r")
               .add_component(ui.Container(component_id="a").set_grow(1))
               .add_component(ui.Container(component_id="b").set_grow(2))
               .add_component(ui.Container(component_id="c").set_grow(1)))
        page = (ui.Page(component_id="p")
                .add_section(ui.Section(component_id="s").add_component(row)))
        registry = DashboardUIRegistry()
        registry.attach("t", ui.Layout(component_id="l").add_page(page))
        # layout → page → section → row
        snap_row = (registry.get_layout("t")["children"][0]
                    ["children"][0]["children"][0])
        self.assertEqual(snap_row["type"], "row")
        self.assertEqual([c.get("grow") for c in snap_row["children"]],
                         [1, 2, 1])


class TestGridComponent(unittest.TestCase):
    """Structured columns: fixed n or auto-fill with a min child width."""

    def _ui(self):
        from core.dashboard_ui import DashboardUIToolset
        return DashboardUIToolset()

    def test_fixed_columns_and_gap(self):
        g = (self._ui().Grid(component_id="g")
             .set_columns(4).set_gap("12px"))
        self.assertEqual(g._props(), {"columns": 4, "gap": "12px"})

    def test_auto_columns_with_min_child(self):
        g = (self._ui().Grid(component_id="g")
             .set_columns("auto").set_min_child("200px"))
        self.assertEqual(g._props(), {"columns": "auto",
                                      "min_child": "200px"})

    def test_column_bounds(self):
        from core.dashboard_ui.errors import UIError
        g = self._ui().Grid(component_id="g")
        for bad in (0, 7, "many"):
            with self.assertRaises(UIError):
                g.set_columns(bad)

    def test_grid_holds_content_children(self):
        ui = self._ui()
        ui.Grid(component_id="g").add_component(
            ui.Container(component_id="c"))
        ui.Grid(component_id="g2").add_component(
            ui.DisplayText(component_id="d").set_value("x"))

    def test_alignment_inherited(self):
        g = (self._ui().Grid(component_id="g")
             .set_align("center").set_justify("between"))
        self.assertEqual((g._props()["align"], g._props()["justify"]),
                         ("center", "between"))

    def test_grid_tree_validates(self):
        from core.dashboard_ui.registry import DashboardUIRegistry
        ui = self._ui()
        page = (ui.Page(component_id="p")
                .add_section(ui.Section(component_id="s").add_component(
                    ui.Grid(component_id="g").set_columns(3)
                    .add_component(ui.DisplayText(component_id="d1")
                                   .set_value("1"))
                    .add_component(ui.DisplayText(component_id="d2")
                                   .set_value("2")))))
        registry = DashboardUIRegistry()
        registry.attach("t", ui.Layout(component_id="l").add_page(page))
        grid = (registry.get_layout("t")["children"][0]
                ["children"][0]["children"][0])
        self.assertEqual(grid["type"], "grid")
        self.assertEqual(len(grid["children"]), 2)


class TestAlertComponent(unittest.TestCase):
    """The designed error/notice surface."""

    def _ui(self):
        from core.dashboard_ui import DashboardUIToolset
        return DashboardUIToolset()

    def test_serialize(self):
        a = (self._ui().Alert(component_id="a")
             .set_title("Boom").set_value("it broke").set_variant("err"))
        self.assertEqual(a._props(), {"title": "Boom", "value": "it broke",
                                      "variant": "err"})

    def test_variant_rejected(self):
        from core.dashboard_ui.errors import UIError
        with self.assertRaises(UIError):
            self._ui().Alert(component_id="a").set_variant("loud")

    def test_alert_anywhere_content_goes(self):
        ui = self._ui()
        ui.Section(component_id="s").add_component(
            ui.Alert(component_id="a1"))
        ui.Container(component_id="c").add_component(
            ui.Alert(component_id="a2"))
        ui.Grid(component_id="g").add_component(ui.Alert(component_id="a3"))


class TestDecorativeLayer(unittest.TestCase):
    """Card tones + Section icons: emphasis that never changes structure."""

    def _ui(self):
        from core.dashboard_ui import DashboardUIToolset
        return DashboardUIToolset()

    def test_card_tone(self):
        from core.dashboard_ui.errors import UIError
        card = self._ui().Card(component_id="c")
        for good in ("info", "ok", "warn", "err"):
            card.set_tone(good)
        with self.assertRaises(UIError):
            card.set_tone("purple")

    def test_section_icon(self):
        s = self._ui().Section(component_id="s").set_icon("M3 12h4")
        self.assertEqual(s._props()["icon"], "M3 12h4")

    def test_client_action_whitelist(self):
        from core.dashboard_ui.errors import UIError
        ui = self._ui()
        ui.Form(component_id="f1").set_client_action("save-token")
        ui.Form(component_id="f2").set_client_action("save-theme")
        with self.assertRaises(UIError):
            ui.Form(component_id="f3").set_client_action("save-anything")


class TestToolbarAndFormGrid(unittest.TestCase):
    """The structure primitives: header bars and non-tower forms."""

    def _ui(self):
        from core.dashboard_ui import DashboardUIToolset
        return DashboardUIToolset()

    def test_toolbar_serializes_title_icon_gap(self):
        tb = (self._ui().Toolbar(component_id="tb")
              .set_title("Tasks").set_icon("M4 6h16").set_gap("8px"))
        self.assertEqual(tb._props(), {"title": "Tasks",
                                       "icon": "M4 6h16", "gap": "8px"})

    def test_toolbar_holds_flow_children_not_rows(self):
        from core.dashboard_ui.errors import InvalidChildError
        ui = self._ui()
        ui.Toolbar(component_id="tb").add_component(
            ui.Button(component_id="b").set_label("go"))
        with self.assertRaises(InvalidChildError):
            ui.Toolbar(component_id="tb2").add_component(
                ui.Row(component_id="r"))

    def test_toolbar_alignment_inherited(self):
        tb = self._ui().Toolbar(component_id="tb").set_align("center")
        self.assertEqual(tb._props()["align"], "center")

    def test_form_columns_int_and_auto(self):
        ui = self._ui()
        f = ui.Form(component_id="f").set_columns(3)
        self.assertEqual(f._props()["columns"], 3)
        f2 = ui.Form(component_id="f2").set_columns("auto")
        self.assertEqual(f2._props()["columns"], "auto")

    def test_form_columns_bounds(self):
        from core.dashboard_ui.errors import UIError
        f = self._ui().Form(component_id="f")
        for bad in (0, 5, "many"):
            with self.assertRaises(UIError):
                f.set_columns(bad)

    def test_stacked_form_omits_columns(self):
        f = self._ui().Form(component_id="f")
        self.assertNotIn("columns", f._props())

    def test_icon_on_every_container(self):
        ui = self._ui()
        for name in ("Section", "Card", "Row", "Column", "Container",
                     "Grid", "Toolbar"):
            inst = getattr(ui, name)(component_id="x")
            self.assertTrue(hasattr(inst, "set_icon"),
                            f"{name} missing set_icon")
