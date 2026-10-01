interfaces — management surfaces
================================

CLI
---
.. automodule:: interfaces.cli.commands
   :members: run_command, help_text, HELP
.. automodule:: interfaces.cli.repl
   :members: Repl, run_once, render_error

Dashboard
---------
.. automodule:: interfaces.dashboard.server
   :members: DashboardServer
.. automodule:: interfaces.dashboard.ws
   :members: decode_frame, handshake_response, accept_key
