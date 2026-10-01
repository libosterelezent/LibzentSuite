Dashboard
=========

``python main.py --dashboard --headless`` serves the SPA + REST + WebSocket
on ``127.0.0.1:8070`` (``DASHBOARD_TOKEN`` enables auth; static page is
served unauthenticated). Sidebar sections mirror the spec: Overview,
Accounts, Plugins, Profiles, Rate Limits (incl. the sequential lane),
Tasks, Logs (live structured tail), Simulation (scenario runner), Advanced
(emergency stop / resume).

Every route maps 1:1 to a :class:`core.api.ControlAPI` method — the CLI
and the dashboard can never diverge in capability. Route table and WS
protocol: ``knowledgebase/DASHBOARD.md``.
