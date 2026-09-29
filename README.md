# MudaeScripts

Selfbot framework for Mudae automation: plugin modules with declared settings,
a centralized multi-account container, a two-layer rate limiter, and two
management surfaces (terminal + GUI dashboard API).

> ⚠️ Selfbots violate Discord's Terms of Service and can get accounts
> suspended. Keep rate rules conservative (defaults: `8/12s`) and never add
> accounts you cannot afford to lose.

## Layout

```
core/       framework: container, rate limiter, plugins, events, credentials,
            reconnect supervisor, persistence, simulator
plugins/    plugin modules (soulcopy = migrated Soulcopy/autocopy.py)
cli/        terminal REPL + full-screen TUI + shared command engine
dashboard/  JSON REST API + WebSocket push + browser SPA (static/)
config/     global settings, rate limits, per-account JSON (no secrets!)
tests/      125-test suite incl. full local simulations
knowledgebase/  local-only, read-only docs (gitignored)
state/      persisted runtime state (gitignored)
main.py     entrypoint
```

## Quickstart

```bash
cp .env.example .env        # fill tokens + OWNER_USER_IDS (chmod 600 .env)
python3 main.py --sim       # watch the framework run locally (no network)
python3 main.py             # terminal management
python3 main.py --tui       # full-screen TUI with live log tailing
python3 main.py --start-all --dashboard   # live accounts + browser dashboard
bash scripts/run_tests.sh   # run the simulation test suite
```

The dashboard serves a browser UI on `http://127.0.0.1:8070` (accounts,
plugin toggles, rate-limit editing, live logs over WebSocket). Set
`DASHBOARD_TOKEN` in `.env` to require authentication.

Runtime changes (plugin toggles, per-bot rate overrides) persist to
`state/state.json` and are restored on the next boot.

Dependencies: Python ≥ 3.10, standard library only for everything local.
Live mode additionally needs a user-account client fork
(`pip install discord.py-self`), imported lazily.

## Credentials & IDs

Everything identifiable lives in `.env` (gitignored, chmod 600):

| Key | Meaning |
|---|---|
| `MUDAE_BOT_ID` | Mudae bot id (used by soulcopy) |
| `OWNER_USER_IDS` | comma-separated user ids allowed to trigger commands |
| `DISCORD_TOKEN_<NAME>` | one token per account in `config/accounts/<name>.json` |
| `DASHBOARD_TOKEN` | local auth token for the dashboard API |

No user/bot ids are hardcoded in the codebase; tests use synthetic fixtures.

## Management surfaces

- Terminal: `help status plugins start/stop limits set-limit enable/disable env quit`
- Dashboard API: `GET /api/status|plugins|limits`, `POST /api/start|stop|plugin|limit|clear-limit`
  (auth header `X-Dashboard-Token`), default `http://127.0.0.1:8070`

## Documentation

`knowledgebase/` holds function-level docs (what each function does, what it's
for, what it expects/returns) — kept read-only on disk (`chmod -R a-w
knowledgebase/`; unlock with `chmod -R u+w` to edit).

## Tests & simulations

`python3 -m unittest discover -s tests -t . -v` — rate limiter math (manual
clock), plugin loader, container routing/isolation, settings layers, soulcopy
text pipeline, end-to-end simulated slcopy runs, reconnect supervisor with
backoff, persistence roundtrips, WebSocket codec + live dashboard over real
HTTP. See `knowledgebase/KB_09_SIMULATOR_TESTING.md`.
