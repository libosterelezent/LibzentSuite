"""The Flet UI — the dashboard replacement (interface pivot phase 3).

A Python-native desktop app that talks to the FastAPI core API. This
module ships the SHELL: the page registry, the app scaffold, and the
client that wraps the REST surface. Pages (accounts, workspaces,
plugins, control plane, system) register here and render into the
shell — one navigation model, the same data the CLI sees.

Run standalone (dev): python -m interfaces.ui
Run embedded (production): flet.app() boots after the FastAPI server.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.request import Request, urlopen

LOG = logging.getLogger("libzent.ui")

__all__ = ["ApiClient", "build_app"]

#: page modules register into this registry (name -> callable building
#: the flet controls); the shell iterates it for the sidebar
PAGES: dict[str, Any] = {}


def page(name: str):
    """Decorator: register a page builder under its sidebar name."""

    def _register(builder: Any) -> Any:
        PAGES[name] = builder
        return builder

    return _register


class ApiClient:
    """Thin REST client over the FastAPI core API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8070", token: str = "") -> None:
        self._base = base_url.rstrip("/")
        self._token = token

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self._base}{path}"
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-API-Token"] = self._token
        data = json.dumps(body).encode() if body else None
        request = Request(url, data=data, headers=headers, method=method)
        with urlopen(request) as response:
            if response.status == 204:
                return None
            return json.loads(response.read())

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: dict | None = None) -> Any:
        return self._request("POST", path, body)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)


def build_app(client: ApiClient):
    """Build the flet application shell with every registered page."""
    import flet as ft

    def _shell(page: ft.Page) -> None:
        page.title = "LibzentSuite"
        page.theme_mode = ft.ThemeMode.DARK
        rail = ft.NavigationRail(
            selected_index=0,
            label_type=ft.NavigationRailLabelType.ALL,
            destinations=[
                ft.NavigationRailDestination(icon=ft.Icons.DASHBOARD, label=name.title())
                for name in PAGES
            ],
        )
        content = ft.Container(expand=True, padding=16)

        def _navigate(e: ft.ControlEvent) -> None:
            if e.control.selected_index is not None:
                name = list(PAGES)[e.control.selected_index]
                builder = PAGES[name]
                content.content = builder(page, client)
                page.update()

        rail.on_change = _navigate
        # initial page
        if PAGES:
            first = next(iter(PAGES))
            content.content = PAGES[first](page, client)

        page.add(
            ft.Row(
                [rail, ft.VerticalDivider(width=1), content],
                expand=True,
            )
        )

    return _shell


# ------------------------------------------------------------------ pages


def _register_default_pages() -> None:
    """Register the built-in pages (imported lazily — flet at runtime)."""

    @page("status")
    def _status_page(page: Any, client: ApiClient) -> Any:
        import flet as ft

        data = client.get("/api/status") or {}

        return ft.Column(
            [
                ft.Text("System", size=24, weight=ft.FontWeight.BOLD),
                ft.Text(f"Status: {data.get('status', 'unknown')}"),
                ft.Text(f"Accounts: {data.get('accounts', 0)}"),
                ft.Text(f"Plugins: {data.get('plugins', 0)}"),
                ft.Text(f"Uptime: {data.get('uptime_s', 0):.0f}s"),
                ft.ElevatedButton(
                    "Emergency Stop",
                    icon=ft.Icons.STOP_CIRCLE,
                    on_click=lambda e: client.post("/api/estop"),
                ),
            ],
            spacing=12,
        )

    @page("accounts")
    def _accounts_page(page: Any, client: ApiClient) -> Any:
        import flet as ft

        accounts = client.get("/api/accounts") or []
        rows = [
            ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(a.get("name", ""))),
                    ft.DataCell(ft.Text(a.get("status", ""))),
                    ft.DataCell(ft.Text(str(a.get("workspace") or "-"))),
                    ft.DataCell(ft.Text(", ".join(a.get("assigned_plugins", [])))),
                ]
            )
            for a in accounts
        ]
        return ft.Column(
            [
                ft.Text("Accounts", size=24, weight=ft.FontWeight.BOLD),
                ft.DataTable(
                    columns=[
                        ft.DataColumn(ft.Text("Name")),
                        ft.DataColumn(ft.Text("Status")),
                        ft.DataColumn(ft.Text("Workspace")),
                        ft.DataColumn(ft.Text("Plugins")),
                    ],
                    rows=rows,
                ),
            ],
            spacing=12,
        )

    @page("workspaces")
    def _workspaces_page(page: Any, client: ApiClient) -> Any:
        import flet as ft

        workspaces = client.get("/api/workspaces") or []
        rows = [
            ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(w.get("name", ""))),
                    ft.DataCell(ft.Text(str(w.get("member_count", 0)))),
                    ft.DataCell(ft.Text("paused" if w.get("paused") else "active")),
                    ft.DataCell(ft.Text(str(w.get("generation", 1)))),
                ]
            )
            for w in workspaces
        ]
        return ft.Column(
            [
                ft.Text("Workspaces", size=24, weight=ft.FontWeight.BOLD),
                ft.DataTable(
                    columns=[
                        ft.DataColumn(ft.Text("Name")),
                        ft.DataColumn(ft.Text("Members")),
                        ft.DataColumn(ft.Text("State")),
                        ft.DataColumn(ft.Text("Generation")),
                    ],
                    rows=rows,
                ),
            ],
            spacing=12,
        )

    @page("logs")
    def _logs_page(page: Any, client: ApiClient) -> Any:
        import flet as ft

        logs = client.get("/api/logs?limit=50") or []
        return ft.Column(
            [
                ft.Text("Logs", size=24, weight=ft.FontWeight.BOLD),
                ft.ListView(
                    [
                        ft.Text(
                            f"[{e.get('severity', 'INFO')}] {e.get('source', '')}"
                            f": {e.get('message', '')}",
                            size=12,
                            selectable=True,
                        )
                        for e in logs
                    ],
                    expand=True,
                ),
            ],
            spacing=12,
        )


_register_default_pages()
