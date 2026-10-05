CLI reference
=============

One command engine (``interfaces/cli/commands.py``) serves both the REPL
(``python main.py``) and one-shot mode (``python main.py accounts list``).
Errors render as one ``error: <message>`` line; every exit path runs the
graceful shutdown.

One-shot exit codes: an empty argv prints help and exits 0; ``quit``
exits 0; ANY command failure — including an unknown command (a usage
error) — prints the one-line error and exits 1. Number arguments are
strictly finite: ``nan``/``inf`` (and non-numeric) tokens are usage
errors, never values.

Groups: ``status`` · ``accounts
list|add|remove|info|credential|enable|start|stop|estop`` · ``credentials
status|import-env`` · ``cooldowns [account]|clear`` · ``plugins
list|state|enable|disable|settings|set`` · ``profiles
list|create|delete|assign|unassign`` · ``ratelimit show|stats|set|clear|effective|
warnings|floor|lane`` · ``tasks list|cancel|history|cancel-matching`` ·
``simulation list|run`` · ``logs [n] [account] [severity]`` · ``audit`` ·
``estop [resume]`` · ``help`` · ``quit``. Bare ``cooldowns [account]``
lists every account's active persistent cooldowns (soonest first).

Full signatures and strict-parsing rules: ``knowledgebase/CLI.md`` (if
you maintain a local knowledgebase — not shipped in a fresh clone).
