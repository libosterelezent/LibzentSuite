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
list|add|plugins|plugin-add|plugin-remove|remove|info|credential|enable|
start|stop|estop`` · ``credentials
status|import-env`` · ``cooldowns [account]|clear`` · ``plugins
list|state|enable|disable|settings|setwarnings|floor|lane`` · ``tasks list|cancel|history|cancel-matching`` ·
``simulation list|run`` · ``log <account> [n] [severity] [--live]`` ·
``global-log [n] [severity] [--live]`` · ``audit`` ·
``estop [resume]`` · ``help`` · ``quit``. Bare ``cooldowns [account]``
lists every account's active persistent cooldowns (soonest first).

Capabilities are DIRECT account→plugin assignments:
``accounts add <name> [plugin ...]`` stores the initial set,
``accounts plugins <account>`` lists it, ``accounts plugin-add`` /
``accounts plugin-remove`` mutate it (idempotent; applied immediately).
Assignment is not enablement — ``plugins enable|disable`` toggles a
plugin per account independently.

Full signatures and strict-parsing rules: ``knowledgebase/CLI.md`` (if
you maintain a local knowledgebase — not shipped in a fresh clone).
