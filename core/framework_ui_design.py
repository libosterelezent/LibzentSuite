"""Design gallery — the framework UI's component sandbox page.

Split out of framework_ui.py: every builder component in every state and
tone on one page (registered as the "Design" tab). Pure builder code with
no dependency on the rest of the framework pages.
"""
from __future__ import annotations

from typing import Any

from core.dashboard_ui import DashboardUIToolset


# ─────────────────────────────────────────────────────── design gallery
def _dg_demo_button(ui, label, cid):
    """A gallery button that DEMONSTRATES the action contract instead of
    erroring with 'declares no action' (a decorative button without an
    action is a dead end for the user)."""
    def _demo(ctx, payload):
        return {"success": True,
                "message": f"{label}: demo action — real buttons wire "
                           f"set_action(callable)"}
    return ui.Button(component_id=cid).set_label(label).set_action(_demo)


def _build_design(ui: DashboardUIToolset, api: Any):
    """The component gallery: every builder component, every state and
    tone, on one page — the sandbox for judging design changes.

    Everything is static (no accounts, no live providers) except the
    interactive demos (row/bulk actions, the modal trigger, the form's
    validation), which run against fixed sample data.
    """
    page = (ui.Page(component_id="design")
            .set_title("Design")
            .set_icon("M12 2l2.4 4.9 5.4.8-3.9 3.8.9 5.4L12 14.5l-4.8 2.4.9-5.4L4.2 7.7l5.4-.8z")
            .set_description(
                "Component gallery — every component, state and tone in "
                "one place. The sandbox for design iterations."))

    # ───────────────────────────────────────────── structure tab
    structure = ui.Tab(component_id="dg-structure").set_title("Structure")

    structure.add_section(
        ui.Section(component_id="dg-sec-containers").set_title("Containers")
        .set_description("Section, Card (every tone), Header, Footer, Separator")
        .add_component(ui.Header(component_id="dg-hdr")
                       .set_title("Header")
                       .set_subtitle("Header.set_subtitle — page-level title block"))
        .add_component(ui.Grid(component_id="dg-grid-cards").set_columns(4).set_gap("8px")
            .add_component(ui.Card(component_id="dg-card-plain").set_title("Card")
                .add_component(ui.Text(component_id="dg-t-plain").set_value("plain")))
            .add_component(ui.Card(component_id="dg-card-info").set_title("info").set_tone("info")
                .add_component(ui.Text(component_id="dg-t-info").set_value("tone=info")))
            .add_component(ui.Card(component_id="dg-card-ok").set_title("ok").set_tone("ok")
                .add_component(ui.Text(component_id="dg-t-ok").set_value("tone=ok")))
            .add_component(ui.Card(component_id="dg-card-warn").set_title("warn").set_tone("warn")
                .add_component(ui.Text(component_id="dg-t-warn").set_value("tone=warn")))
            .add_component(ui.Card(component_id="dg-card-err").set_title("err").set_tone("err")
                .add_component(ui.Text(component_id="dg-t-err").set_value("tone=err"))))
        .add_component(ui.Card(component_id="dg-card-layout")
            .set_title("Rows, Columns, Grids")
            .add_component(ui.Row(component_id="dg-row1")
                .add_component(_dg_demo_button(ui, "Row item 1", "dg-b1"))
                .add_component(_dg_demo_button(ui, "Row item 2", "dg-b2"))
                .add_component(_dg_demo_button(ui, "Row item 3", "dg-b3")))
            .add_component(ui.Row(component_id="dg-row2").set_gap("16px")
                .add_component(ui.Text(component_id="dg-t-gap").set_value("gap=16px row"))
                .add_component(ui.Text(component_id="dg-t-gap2").set_value("second cell")))
            .add_component(ui.Grid(component_id="dg-grid-auto").set_columns("auto").set_min_child("180px")
                .add_component(ui.Card(component_id="dg-gc1").add_component(ui.Text(component_id="dg-gt1").set_value("auto grid cell")))
                .add_component(ui.Card(component_id="dg-gc2").add_component(ui.Text(component_id="dg-gt2").set_value("auto grid cell")))
                .add_component(ui.Card(component_id="dg-gc3").add_component(ui.Text(component_id="dg-gt3").set_value("auto grid cell"))))
            .add_component(ui.Container(component_id="dg-cont-v")
                .add_component(ui.Text(component_id="dg-t-cv").set_value("Container (vertical)"))
                .add_component(ui.Text(component_id="dg-t-cv2").set_value("stacked children")))
            .add_component(ui.Container(component_id="dg-cont-h").set_direction("horizontal")
                .add_component(_dg_demo_button(ui, "Horizontal", "dg-b-h1"))
                .add_component(_dg_demo_button(ui, "container", "dg-b-h2"))
                .add_component(_dg_demo_button(ui, "children", "dg-b-h3"))))
        .add_component(ui.Separator(component_id="dg-sep"))
        .add_component(ui.Separator(component_id="dg-sep-dash").set_variant("dashed"))
        .add_component(ui.Description(component_id="dg-desc")
                       .set_title("Description")
                       .set_value("Description — a term/definition pair for metadata lists."))
        .add_component(ui.Footer(component_id="dg-foot")
                       .set_value("Footer — page-bottom fine print")))

    structure.add_section(
        ui.Section(component_id="dg-sec-toolbar").set_title("Toolbar & Buttons")
        .set_description("Toolbar, Button variants, disabled state")
        .add_component(ui.Toolbar(component_id="dg-toolbar").set_title("Toolbar")
            .add_component(_dg_demo_button(ui, "↻ Refresh", "dg-tb-refresh"))
            .add_component(_dg_demo_button(ui, "Delete", "dg-tb-danger").set_variant("danger")))
        .add_component(ui.Row(component_id="dg-row-btns")
            .add_component(_dg_demo_button(ui, "Plain", "dg-btn-plain"))
            .add_component(_dg_demo_button(ui, "Primary", "dg-btn-primary").set_variant("primary"))
            .add_component(_dg_demo_button(ui, "Danger", "dg-btn-danger").set_variant("danger"))
            .add_component(ui.Button(component_id="dg-btn-disabled").set_label("Disabled").set_disabled(True))
            .add_component(ui.Button(component_id="dg-btn-modal").set_label("Open modal…").set_open_modal("dg-modal"))))

    page.add_modal(ui.Modal(component_id="dg-modal").set_title("Modal")
                   .set_description("Modal — viewport-centered dialog with form chrome.")
                   .add_component(ui.Text(component_id="dg-modal-text")
                                  .set_value("Modal body: any component nests here."))
                   .add_component(ui.Button(component_id="dg-modal-close").set_label("Close")
                                  .set_action(lambda ctx, payload: {"success": True,
                                                                    "message": "modal closed"})))

    page.add_tab(structure)

    # ───────────────────────────────────────────── display tab
    display = ui.Tab(component_id="dg-display").set_title("Display")

    display.add_section(
        ui.Section(component_id="dg-sec-text").set_title("Text & Code")
        .set_description("Text variants, DisplayText sizes × tones, Code")
        .add_component(ui.Text(component_id="dg-tx-plain").set_value("Text — plain body copy"))
        .add_component(ui.Text(component_id="dg-tx-dim").set_value("Text variant=muted").set_variant("muted"))
        .add_component(ui.Text(component_id="dg-tx-code").set_value("Text variant=code").set_variant("code"))
        .add_component(ui.Separator(component_id="dg-sep2"))
        .add_component(ui.Grid(component_id="dg-grid-dt").set_columns(3).set_gap("8px")
            .add_component(ui.DisplayText(component_id="dg-dt-xs").set_value("xs dim").set_size("xs").set_tone("dim"))
            .add_component(ui.DisplayText(component_id="dg-dt-sm").set_value("sm dim").set_size("sm").set_tone("dim"))
            .add_component(ui.DisplayText(component_id="dg-dt-md").set_value("md text").set_size("md"))
            .add_component(ui.DisplayText(component_id="dg-dt-lg").set_value("lg ok").set_size("lg").set_tone("ok"))
            .add_component(ui.DisplayText(component_id="dg-dt-xl").set_value("xl warn").set_size("xl").set_tone("warn"))
            .add_component(ui.DisplayText(component_id="dg-dt-xl-err").set_value("xl err").set_size("xl").set_tone("err")))
        .add_component(ui.Text(component_id="dg-code-block").set_variant("code")
                       .set_value("Code block — bounded, scrollable pane:\nline two\nline three")))

    display.add_section(
        ui.Section(component_id="dg-sec-badges").set_title("Badges & Metrics")
        .set_description("Badge variants, Metric levels, Progress")
        .add_component(ui.Row(component_id="dg-row-badges")
            .add_component(ui.Badge(component_id="dg-bdg-info").set_value("info").set_variant("info"))
            .add_component(ui.Badge(component_id="dg-bdg-ok").set_value("ok").set_variant("ok"))
            .add_component(ui.Badge(component_id="dg-bdg-warn").set_value("warn").set_variant("warn"))
            .add_component(ui.Badge(component_id="dg-bdg-err").set_value("err").set_variant("err")))
        .add_component(ui.Grid(component_id="dg-grid-metrics").set_columns(4).set_gap("8px")
            .add_component(ui.Metric(component_id="dg-m-plain").set_label("accounts").set_value("12"))
            .add_component(ui.Metric(component_id="dg-m-ok").set_label("healthy").set_value("9").set_level("ok"))
            .add_component(ui.Metric(component_id="dg-m-warn").set_label("degraded").set_value("2").set_level("warn"))
            .add_component(ui.Metric(component_id="dg-m-err").set_label("failed").set_value("1").set_level("err")))
        .add_component(ui.Metric(component_id="dg-m-sub").set_label("with sub")
                       .set_value("3.2k").set_sub("≈ 42/min sustained").set_level("ok"))
        .add_component(ui.Progress(component_id="dg-prog").set_value(62).set_label("progress 62%"))
        .add_component(ui.Progress(component_id="dg-prog-0").set_value(0).set_label("progress 0%"))
        .add_component(ui.Progress(component_id="dg-prog-100").set_value(100).set_label("progress 100%")))

    display.add_section(
        ui.Section(component_id="dg-sec-alerts").set_title("Alerts")
        .set_description("Every alert variant")
        .add_component(ui.Alert(component_id="dg-al-info").set_title("Info").set_variant("info")
                       .set_value("Something worth knowing."))
        .add_component(ui.Alert(component_id="dg-al-ok").set_title("Success").set_variant("ok")
                       .set_value("The operation completed."))
        .add_component(ui.Alert(component_id="dg-al-warn").set_title("Warning").set_variant("warn")
                       .set_value("Attention may be required."))
        .add_component(ui.Alert(component_id="dg-al-err").set_title("Error").set_variant("err")
                       .set_value("The operation failed.")))

    page.add_tab(display)

    # ───────────────────────────────────────────── inputs tab
    inputs = ui.Tab(component_id="dg-inputs").set_title("Inputs")

    form = (ui.Form(component_id="dg-form").set_submit_label("Submit demo form")
            .set_submit_action(_dg_form_submit))
    form.add_field(ui.TextInput(component_id="dg-in-text")
                       .set_label("TextInput").set_placeholder("type here…")
                       .set_required(True).set_max_length(40))
    form.add_field(ui.TextInput(component_id="dg-in-disabled")
                       .set_label("TextInput (disabled)").set_value("not editable")
                       .set_disabled(True))
    form.add_field(ui.NumberInput(component_id="dg-in-num")
                       .set_label("NumberInput").set_value("7")
                       .set_min_value(0).set_max_value(60).set_step(1))
    form.add_field(ui.Textarea(component_id="dg-in-area")
                       .set_label("Textarea").set_placeholder("multi-line…").set_rows(3))
    form.add_field(ui.Select(component_id="dg-in-select").set_label("Select")
                       .set_placeholder("choose…")
                       .set_options([{"value": "a", "label": "Option A"},
                                     {"value": "b", "label": "Option B"},
                                     {"value": "c", "label": "Option C"}]))
    form.add_field(ui.MultiSelect(component_id="dg-in-multi").set_label("MultiSelect")
                       .set_options([{"value": "x", "label": "Choice X"},
                                     {"value": "y", "label": "Choice Y"},
                                     {"value": "z", "label": "Choice Z"}])
                       .set_value(["x"]))
    form.add_field(ui.Checkbox(component_id="dg-in-check").set_label("Checkbox")
                       .set_description("a boolean choice").set_value(True))
    form.add_field(ui.Toggle(component_id="dg-in-toggle").set_label("Toggle")
                       .set_description("the switch-skinned boolean"))
    form.add_field(ui.RadioGroup(component_id="dg-in-radio").set_label("RadioGroup")
                       .set_options([{"value": "one", "label": "One"},
                                     {"value": "two", "label": "Two"}])
                       .set_value("one"))

    inputs.add_section(
        ui.Section(component_id="dg-sec-form").set_title("Form — every input")
        .set_description("Required validation fires on submit; disabled states shown")
        .add_component(form))

    page.add_tab(inputs)

    # ───────────────────────────────────────────── data tab
    data = ui.Tab(component_id="dg-data").set_title("Data")

    async def _rows(ctx=None):
        return {"columns": ["Name", "Status", "Seen"],
                "rows": [["alpha", "online", "2m"], ["beta", "offline", "1h"],
                         ["gamma", "cooldown", "9m"], ["delta", "online", "now"]]}

    async def _records(ctx=None):
        tones = {"online": "ok", "offline": "muted", "cooldown": "warn"}
        return {"records": [
            {"key": name, "cells": [
                {"primary": name, "secondary": seen},
                {"text": status, "tone": tones.get(status, "muted")},
                {"chips": ["probe"]}, "—"]}
            for name, status, seen in [
                ("alpha", "online", "2m"), ("beta", "offline", "1h"),
                ("gamma", "cooldown", "9m")]]}

    async def _no_records(ctx=None):
        return {"records": []}

    async def _tiles(ctx=None):
        return [{"title": "libo", "status": "online", "meta": "12 plugins",
                 "chips": ["probe", "soulcopy"], "lines": [["uptime", "3h 12m"]]},
                {"title": "libalt30", "status": "cooldown", "meta": "8 plugins",
                 "chips": ["probe"], "lines": [["uptime", "41m"]], "tone": "warn"},
                {"title": "libalt21", "status": "offline", "meta": "—",
                 "chips": [], "lines": [["uptime", "—"]], "tone": "err"}]

    def _poke(row, ctx):
        return {"success": True, "message": f"poked {row}"}

    data.add_section(
        ui.Section(component_id="dg-sec-table").set_title("Table")
        .set_description("Row actions + bulk actions + pagination (page size 2)")
        .add_component(ui.Table(component_id="dg-table").set_columns(["Name", "Status", "Seen"])
                       .set_data_source(_rows).set_page_size(2)
                       .set_row_actions([ui.RowAction("poke", "Poke", _poke)])
                       .set_bulk_actions([ui.BulkAction("poke-all", "Poke all",
                                                        lambda rows, ctx: {"success": True,
                                                                           "message": f"poked {len(rows)}"})])))

    data.add_section(
        ui.Section(component_id="dg-sec-tiles").set_title("TileView")
        .set_description("Status pills, meta rows, chips, tone borders")
        .add_component(ui.TileView(component_id="dg-tiles").set_data_source(_tiles)))

    data.add_section(
        ui.Section(component_id="dg-sec-records").set_title("ListView — records mode")
        .set_description("The record-list contract: identity | status | meta | actions")
        .add_component(ui.ListView(component_id="dg-records")
                       .set_columns(["Account", "Status", "Plugins", "Group"])
                       .set_key_column("Account")
                       .set_data_source(_records)
                       .set_empty_text("no records")))

    data.add_section(
        ui.Section(component_id="dg-sec-empty").set_title("Empty states")
        .set_description("Every data component's empty_text")
        .add_component(ui.Table(component_id="dg-table-empty").set_columns(["A", "B"])
                       .set_data_source(lambda ctx=None: {"columns": ["A", "B"], "rows": []})
                       .set_empty_text("nothing here yet")))

    page.add_tab(data)
    return page


async def _dg_form_submit(ctx, payload):
    """The gallery form's validation demo: a missing required field
    surfaces as a field error; anything else succeeds."""
    if not str(payload.get("dg-in-text") or "").strip():
        return {"success": False, "field_errors": {
            "dg-in-text": "This field is required (validation demo)."}}
    return {"success": True,
            "message": f"submitted: {sorted(payload)[:3]}…",
            "refresh": []}


