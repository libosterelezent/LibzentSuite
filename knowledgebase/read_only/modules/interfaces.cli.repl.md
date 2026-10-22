# Module `interfaces.cli.repl`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `interfaces\cli\repl.py`

```
Interactive REPL + one-shot mode for the CLI surface (spec §13).

Thin shell over :mod:`interfaces.cli.commands` — the shared engine — with the
error rendering and every-exit-path shutdown ported from the audited old
``cli/terminal.py``:

  * ``api.shutdown()`` and the ``bye.`` line run on EVERY exit path (quit,
    EOF, Ctrl-C at the prompt, Ctrl-C mid-command);
  * command failures NEVER leak tracebacks — exactly one ``error: ...`` line
    each, with the exception type name kept only for non-domain errors;
  * stdout goes ONLY through the injected stream (no logging side effects).

Decisions for things INTEGRATION.md left open:
  * ``run_once`` is a coroutine (``run_command`` awaits the async facade, so
    it must run inside the caller's event loop — typically
    ``asyncio.run(run_once(api, argv))`` from ``main.py``);
  * ``run_once`` does NOT call ``api.shutdown()`` — the one-shot lifecycle is
    owned by ``main.py``; ``quit`` there just prints ``bye.`` and returns 0.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `Repl` | class | yes | Interactive loop over the ControlAPI; injectable stdin/stdout for tests. |

### `Repl`

- `async def run(self) -> None` — side effects: `creates_tasks, publishes_event`
  - Main loop; ``api.shutdown()`` and ``bye.`` run on every exit path.
- `async def execute(self, line: str) -> bool`
  - Run one command line; returns False when the REPL should exit.

## Functions

- `def render_error(exc: BaseException) -> str`
  - One-line error text; keeps the type name for non-domain exceptions.
- `async def run_once(api: Any, argv: list, stdout: Optional[TextIO]=None) -> int`
  - One-shot mode: run a single command line built from ``argv``.
