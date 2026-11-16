"""Flet UI tests — the shell, the client, and the page registry."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

from interfaces.ui import PAGES, ApiClient, page


class ApiClientTests(unittest.TestCase):
    def test_get_fetches_json(self) -> None:
        client = ApiClient(base_url="http://x", token="t")
        with mock.patch("interfaces.ui.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value = mock.MagicMock(
                status=200, read=lambda: json.dumps({"ok": True}).encode()
            )
            result = client.get("/api/status")
        self.assertEqual(result, {"ok": True})

    def test_post_sends_body(self) -> None:
        client = ApiClient(base_url="http://x", token="t")
        with mock.patch("interfaces.ui.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value = mock.MagicMock(
                status=200, read=lambda: b"{}"
            )
            client.post("/api/accounts", {"name": "beta"})
            request = mock_open.call_args[0][0]
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.full_url, "http://x/api/accounts")
            self.assertEqual(request.get_header("X-api-token"), "t")


class PageRegistryTests(unittest.TestCase):
    def test_default_pages_registered(self) -> None:
        for name in ("status", "accounts", "workspaces", "logs"):
            self.assertIn(name, PAGES, name)

    def test_decorator_registers(self) -> None:
        @page("custom")
        def _builder(page_ref: Any, client: Any) -> None:
            return None

        self.assertIn("custom", PAGES)
        del PAGES["custom"]  # cleanup


if __name__ == "__main__":
    unittest.main()
