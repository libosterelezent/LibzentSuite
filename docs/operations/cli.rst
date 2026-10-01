CLI reference
=============

One command engine (``interfaces/cli/commands.py``) serves both the REPL
(``python main.py``) and one-shot mode (``python main.py accounts list``).
Errors render as one ``error: <message>`` line; every exit path runs the
graceful shutdown.

Groups: ``status`` · ``accounts list|add|remove|info|enable|start|stop|estop``
· ``plugins list|state|enable|disable|settings|set`` · ``profiles
list|create|delete|assign|unassign`` · ``ratelimit show|set|clear|effective|
warnings|floor|lane`` · ``tasks list|cancel|history|cancel-matching`` ·
``simulation list|run`` · ``logs`` · ``audit`` · ``estop [resume]`` ·
``help`` · ``quit``.

Full signatures and strict-parsing rules: ``knowledgebase/CLI.md``.
