"""Stage-D dashboard UI routes over real HTTP: the full path — probe
fixture → PluginManager lifecycle → DashboardUIRegistry → ControlAPI →
DashboardServer routes → JSON.

Unlike test_dashboard_http.py (duck-typed canned API), this suite builds
the REAL ControlAPI over a REAL PluginManager running the probe fixture,
so the builder → layout → attach → navigation → page → action dispatch
chain is exercised exactly as production wires it. A second, registry-
level layout (builders used inline) provides the data-provider 200 path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from core.api import ControlAPI
from core.dashboard_ui import DashboardUIRegistry, DashboardUIToolset
from core.event_bus import EventBus
from core.plugin_manager import PluginManager
from core.settings_manager import SettingsManager
from interfaces.dashboard.server import DashboardServer

TOKEN = "dui-route-token"
LOG = logging.getLogger("t")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"


class FakeServices:
    """Minimal PluginServices stand-in for activation (lifecycle test's)."""

    def __init__(self, account="alpha") -> None:
        self.account = account

    async def send_message(self, channel_id, content, *, priority=1):
        return None

    async def send_dm(self, user_id, content, *, priority=1):
        return None

    @property
    def settings(self):
        return {}


def http_get(url: str, token: str | None = TOKEN) -> tuple[int, str]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Dashboard-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def http_post(url: str, payload, token: str | None = TOKEN) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Dashboard-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def get_json(text: str) -> dict:
    return json.loads(text)


def _find_comp(tree: dict, type_name: str) -> dict | None:
    """First component of `type_name` in the serialized tree (BFS)."""
    queue = [tree]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            if node.get("type") == type_name:
                return node
            queue.extend(node.get("children") or [])
    return None


async def _provider(ctx) -> list:
    """Data source for the registry-level provider layout."""
    return [{"label": "one", "value": "1"}, {"label": "two", "value": "2"}]


def _provider_layout() -> "DashboardUIToolset.Layout":
    ui = DashboardUIToolset()
    page = ui.Page().set_id("data-page").set_title("Data Demo")
    section = ui.Section().set_title("Live")
    section.add_component(
        ui.ListView(component_id="demo-list")
        .set_data_source(_provider)
        .set_empty_text("no rows"))
    section.add_component(
        ui.Table(component_id="demo-table")
        .set_columns(["name", "count"])
        .set_data_source(lambda ctx: {"columns": ["name", "count"],
                                      "rows": [["a", 1], ["b", 2]]}))
    page.add_section(section)
    return ui.Layout().add_page(page)


class DashboardUIRouteTests(unittest.IsolatedAsyncioTestCase):
    """The four Stage-D routes against the real registry + fixture."""

    async def asyncSetUp(self) -> None:
        self.registry = DashboardUIRegistry()
        bus = EventBus()
        self.manager = PluginManager(
            bus, LOG, SettingsManager(bus),
            plugin_dir=str(FIXTURES),
            ui_registry=self.registry)
        # the real lifecycle path: discovery → instantiation →
        # initialize_dashboard_ui hook → attach
        await self.manager.attach_account("alpha", {"probe"})
        await self.manager.activate_account("alpha", FakeServices("alpha"))
        # a second, registry-level UI carrying data providers
        self.registry.attach("datademo", _provider_layout())
        # the REAL ControlAPI — its dashboard_ui_* methods only touch
        # plugins.ui_registry, which the real manager exposes
        self.api = ControlAPI(
            accounts=object(), plugins=self.manager, profiles=object(),
            settings=object(), limiter=object(), scheduler=object(),
            logs=object())
        self.server = DashboardServer(self.api, token=TOKEN, port=0)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.bound_port}"

    async def asyncTearDown(self) -> None:
        self.server.stop()

    def url(self, path: str) -> str:
        return f"{self.base}{path}"

    async def get(self, path: str, token: str | None = TOKEN) -> tuple:
        return await asyncio.to_thread(http_get, self.url(path), token)

    async def post(self, path: str, payload, token: str | None = TOKEN) -> tuple:
        return await asyncio.to_thread(http_post, self.url(path), payload, token)

    # ------------------------------------------------------------ navigation
    async def test_navigation_lists_fixture_page(self) -> None:
        status, body = await self.get("/api/dashboard-ui/navigation")
        self.assertEqual(status, 200)
        payload = get_json(body)
        self.assertTrue(payload["ok"])
        entries = payload["data"]
        by_key = {(e["plugin"], e["page_id"]): e for e in entries}
        self.assertIn(("probe", "probe-ui"), by_key)
        entry = by_key[("probe", "probe-ui")]
        self.assertEqual(entry["title"], "Probe UI")
        self.assertEqual(entry["order"], 0)
        # the provider layout's page is listed too, ordered after probe
        self.assertIn(("datademo", "data-page"), by_key)
        # entries are sorted by (order, plugin) — both pages are order 0,
        # so the alphabetical plugin name decides
        keys = [(e["order"], e["plugin"]) for e in entries]
        self.assertEqual(keys, sorted(keys))
        self.assertLess(entries.index(by_key[("datademo", "data-page")]),
                        entries.index(by_key[("probe", "probe-ui")]))

    async def test_navigation_empty_registry(self) -> None:
        empty = DashboardUIRegistry()
        manager = PluginManager(
            EventBus(), LOG, SettingsManager(EventBus()),
            plugin_dir=str(FIXTURES), ui_registry=empty)
        api = ControlAPI(accounts=object(), plugins=manager,
                         profiles=object(), settings=object(),
                         limiter=object(), scheduler=object(), logs=object())
        server = DashboardServer(api, token=TOKEN, port=0)
        server.start()
        try:
            status, body = await asyncio.to_thread(
                http_get, f"http://127.0.0.1:{server.bound_port}"
                          "/api/dashboard-ui/navigation")
            self.assertEqual(status, 200)
            payload = get_json(body)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"], [])
        finally:
            server.stop()

    # ----------------------------------------------------------------- page
    async def test_page_returns_component_tree(self) -> None:
        status, body = await self.get(
            "/api/dashboard-ui/page?plugin=probe&page_id=probe-ui")
        self.assertEqual(status, 200)
        page = get_json(body)["data"]
        self.assertEqual((page["type"], page["id"]), ("page", "probe-ui"))
        self.assertEqual(page["title"], "Probe UI")
        # the serialized tree carries the whole fixture page
        section = _find_comp(page, "section")
        self.assertIsNotNone(section)
        self.assertEqual(section["title"], "Controls")
        card = _find_comp(page, "card")
        self.assertEqual(card["title"], "Actions")
        button = _find_comp(page, "button")
        self.assertEqual(button["label"], "Run Probe")
        self.assertEqual(button["variant"], "primary")
        self.assertIs(button["action"], True)  # callable never crosses the wire
        metric = _find_comp(page, "metric")
        self.assertEqual((metric["label"], metric["value"], metric["level"]),
                         ("Status", "ready", "ok"))
        checkbox = _find_comp(page, "checkbox")
        self.assertEqual((checkbox["label"], checkbox["value"]),
                         ("Verbose", False))
        self.assertIs(checkbox["action"], True)

    async def test_page_unknown_ids_404(self) -> None:
        for query in ("plugin=probe&page_id=nope",
                      "plugin=ghost&page_id=probe-ui"):
            status, body = await self.get(
                "/api/dashboard-ui/page?" + query)
            self.assertEqual(status, 404, query)
            self.assertFalse(get_json(body)["ok"])

    async def test_page_requires_query_params(self) -> None:
        for query in ("", "?plugin=probe", "?page_id=probe-ui"):
            status, body = await self.get(
                "/api/dashboard-ui/page" + query)
            self.assertEqual(status, 400, query)
            self.assertFalse(get_json(body)["ok"])

    # --------------------------------------------------------------- action
    async def test_action_dispatches_button(self) -> None:
        _, body = await self.get(
            "/api/dashboard-ui/page?plugin=probe&page_id=probe-ui")
        button = _find_comp(get_json(body)["data"], "button")
        status, body = await self.post("/api/dashboard-ui/action", {
            "plugin": "probe",
            "component_id": button["id"],
            "payload": {"x": 1},
        })
        self.assertEqual(status, 200)
        result = get_json(body)["data"]
        # the full ActionResult shape crosses the wire
        self.assertEqual(sorted(result),
                         ["data", "field_errors", "message", "refresh",
                          "success"])
        self.assertTrue(result["success"])
        self.assertEqual(result["message"], "probe executed")
        self.assertIsInstance(result["data"]["ts"], float)
        self.assertEqual(result["field_errors"], {})
        self.assertEqual(result["refresh"], [])

    async def test_action_dispatches_checkbox_payload(self) -> None:
        _, body = await self.get(
            "/api/dashboard-ui/page?plugin=probe&page_id=probe-ui")
        checkbox = _find_comp(get_json(body)["data"], "checkbox")
        status, body = await self.post("/api/dashboard-ui/action", {
            "plugin": "probe",
            "component_id": checkbox["id"],
            "payload": {"value": True},
        })
        self.assertEqual(status, 200)
        result = get_json(body)["data"]
        self.assertTrue(result["success"])
        self.assertEqual(result["message"], "verbose → True")

    async def test_action_unknown_component_400(self) -> None:
        status, body = await self.post("/api/dashboard-ui/action", {
            "plugin": "probe", "component_id": "missing",
            "payload": {},
        })
        self.assertEqual(status, 400)
        self.assertIn("missing", get_json(body)["error"])

    async def test_action_unknown_plugin_400(self) -> None:
        status, body = await self.post("/api/dashboard-ui/action", {
            "plugin": "ghost", "component_id": "x", "payload": {},
        })
        self.assertEqual(status, 400)

    async def test_action_requires_fields(self) -> None:
        for payload in ({"plugin": "probe"},  # no component_id
                        {"component_id": "x"},  # no plugin
                        {}):
            status, body = await self.post("/api/dashboard-ui/action", payload)
            self.assertEqual(status, 400, payload)
            self.assertFalse(get_json(body)["ok"])

    async def test_action_non_object_payload_400(self) -> None:
        status, body = await self.post("/api/dashboard-ui/action", {
            "plugin": "probe", "component_id": "x", "payload": "oops",
        })
        self.assertEqual(status, 400)
        self.assertIn("payload", get_json(body)["error"])

    # ----------------------------------------------------------------- data
    async def test_data_provider_list(self) -> None:
        status, body = await self.get(
            "/api/dashboard-ui/data?plugin=datademo&component_id=demo-list")
        self.assertEqual(status, 200)
        data = get_json(body)["data"]
        self.assertEqual(data, [{"label": "one", "value": "1"},
                                {"label": "two", "value": "2"}])

    async def test_data_provider_table_shape(self) -> None:
        status, body = await self.get(
            "/api/dashboard-ui/data?plugin=datademo&component_id=demo-table")
        self.assertEqual(status, 200)
        data = get_json(body)["data"]
        self.assertEqual(data["columns"], ["name", "count"])
        self.assertEqual(data["rows"], [["a", 1], ["b", 2]])

    async def test_data_no_provider_404(self) -> None:
        # the fixture's button declares an action, not a data source
        _, body = await self.get(
            "/api/dashboard-ui/page?plugin=probe&page_id=probe-ui")
        button = _find_comp(get_json(body)["data"], "button")
        status, body = await self.get(
            "/api/dashboard-ui/data?plugin=probe&component_id=" + button["id"])
        self.assertEqual(status, 404)
        self.assertFalse(get_json(body)["ok"])
        self.assertIn("provider", get_json(body)["error"])

    async def test_data_unknown_component_404(self) -> None:
        status, body = await self.get(
            "/api/dashboard-ui/data?plugin=probe&component_id=missing")
        self.assertEqual(status, 404)
        status, body = await self.get(
            "/api/dashboard-ui/data?plugin=ghost&component_id=missing")
        self.assertEqual(status, 404)

    async def test_data_requires_query_params(self) -> None:
        status, body = await self.get("/api/dashboard-ui/data")
        self.assertEqual(status, 400)

    # ----------------------------------------------------------------- auth
    async def test_routes_require_token(self) -> None:
        status, _ = await self.get("/api/dashboard-ui/navigation", None)
        self.assertEqual(status, 401)
        status, _ = await self.post("/api/dashboard-ui/action", {}, "wrong")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
