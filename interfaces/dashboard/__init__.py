"""interfaces.dashboard — HTTP+WebSocket dashboard (spec §12, agent A8).

Exports the stdlib-only dashboard server: ThreadingHTTPServer REST facade
over ``core.api.ControlAPI`` plus an RFC 6455 subset for live push.
"""
from .server import DashboardServer

__all__ = ["DashboardServer"]
