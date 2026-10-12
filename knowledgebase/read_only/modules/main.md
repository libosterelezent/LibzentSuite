# Module `main`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `main.py`

```
SlaughterMudae entrypoint.

Usage:
  python main.py                        interactive REPL (live accounts from config/)
  python main.py <command ...>          one-shot CLI (e.g. `accounts list`, `status`)
  python main.py --sim [name]           run a simulation scenario locally, no network
  python main.py --sim-list             list available scenarios
  python main.py --start-all            boot every enabled account, then REPL
  python main.py --dashboard --port N   also serve the browser dashboard (+WS)
  python main.py --headless             run forever with no terminal (daemon mode)
```

## Functions

- `async def run_sim(name: str | None, list_only: bool) -> int`
  - Run scenario(s) through the offline SimulationRunner (no network).
- `async def run_app(args: argparse.Namespace, argv: list[str]) -> int` — side effects: `creates_tasks`
  - Boot the live stack and attach the requested surfaces.
- `def main() -> int`
  - Entrypoint: parse argv and dispatch to the simulation runner
