Dashboard
=========

``python main.py --dashboard --headless`` serves the SPA + REST + WebSocket
on ``127.0.0.1:8070`` (``DASHBOARD_TOKEN`` enables auth; static page is
served unauthenticated). Sidebar sections mirror the spec: Overview,
Accounts, Plugins (per-account enablement, settings, the profile
only; capabilities are direct account→plugin assignments), Rate Limits
(incl. the sequential lane),
Cooldowns (active persistent cooldowns, soonest first), Tasks, Logs (live
structured tail), Simulation (scenario runner), Advanced
(emergency stop, .env import, shutdown — resume after an estop is `POST /api/resume` or CLI `estop resume` (no panel button)).

Every route maps 1:1 to a :class:`core.api.ControlAPI` method — the CLI
and the dashboard can never diverge in capability. Route table and WS
protocol: ``knowledgebase/DASHBOARD.md`` (if you maintain a local
knowledgebase — not shipped in a fresh clone).
