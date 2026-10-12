# SlaughterMudae knowledgebase

Handwritten reference documentation for the framework, backed by the
generated symbol inventory. **Source code is the source of truth** — every
table here was verified against it, but when this docs lags the code, the
code wins (and the lag belongs in `DISCREPANCIES.md`).

> ⚠️ Selfbot usage violates Discord's Terms of Service. The safety systems
> (pacing with learned defaults, jitter, server-limit handling) mitigate
> risk — they do not authorize it. See [SECURITY.md](SECURITY.md).

## Directory map

```
knowledgebase/
  README.md            this landing page (handwritten)

  ── Operations / interface reference ────────────────────────────────
  API_ENDPOINTS.md     THE COMPLETE endpoint reference (every REST route,
                       WS protocol, CLI grammar, self-command surface)
  CONFIGURATION.md     COMPLETE settings reference (.env, config/*.json,
                       ACCOUNT_SETTINGS, plugin layering + profiles,
                       env→DB admin migration, DB state)
  CLI.md               the real command surface (HELP registry, strict
                       parsing, REPL vs one-shot)
  DASHBOARD.md         REST route table, WS protocol, SPA, security model
  TESTING.md           suite layout + inventory, determinism, scenarios
  SECURITY.md          credentials, token gates, exposure model, posture
  GLOSSARY.md          every domain term
  AGENT_GUIDE.md       repo map + working rules for coding agents
  CHANGELOG.md         themed history from the real git log

  ── Architecture / internals (companion pages) ──────────────────────
  PROJECT_OVERVIEW.md  what the framework is, at a glance
  ARCHITECTURE.md      design principles (source-verified)
  COMPONENT_MAP.md     who owns what across the managers
  DATA_FLOW.md         the core dataflows end-to-end
  RUNTIME_LIFECYCLE.md boot → run → shutdown narrative
  ACCOUNT_SYSTEM.md    accounts (persistent/runtime split) deep-dive
  PLUGIN_SYSTEM.md     the plugin contract + manager
  PLUGIN_API.md        full plugin API reference
  PLUGIN_EXAMPLES.md   worked plugin examples
  PLUGIN_ANTIPATTERNS.md  what NOT to do in a plugin
  EVENT_BUS.md         typed pub/sub + handler isolation
  SCHEDULER.md         the centralized task pipeline
  BENCHMARKING.md      the benchmark subsystem (scenarios, metrics,
                       profiling modes, baselines/compare)
  RATE_LIMITING.md     pacing, policies, server limits, send lane
  LOGGING.md           the two-pipeline structured logging architecture
  ERROR_HANDLING.md    the domain exception taxonomy
  DATABASE.md          SQLite persistence: migrations + repositories
  ADAPTERS.md          the Discord boundary (live + simulator)
  plugins/             the plugin map (index.md — the framework ships
                       with NO bundled plugins)
  DISCREPANCIES.md     doc/implementation mismatches (append-only)

  read_only/           GENERATED reference — do not hand-edit
    SYMBOL_INDEX.md      per-module symbol table
    modules/<mod>.md     per-module signatures + docstrings
    plugins/<name>.md    per-plugin manifests
  generated/           GENERATED data (machine-readable)
    symbol_index.json      symbol inventory (public/plugin_api flags)
    module_dependencies.json  import graph
    plugins.json           plugin manifests
    coverage_report.json   measured docstring coverage
    snapshot_manifest.json commit/python/counts metadata
    drift_baseline.json    symbol hashes for drift detection
```

Handwritten files are maintained by hand; `read_only/` and `generated/` are
**overwritten** by the generator (READ ONLY is a convention, not filesystem
enforcement).

## Regenerate + drift-check

```bash
python3 scripts/gen_knowledgebase.py            # regenerate everything
python3 scripts/gen_knowledgebase.py --check    # drift report vs baseline
                                                # (exit 2 on any drift)
python3 scripts/gen_knowledgebase.py --skip-plugins  # skip the controlled
                                                # plugin-manifest import
```

The generator is static AST inspection (no project code executes except a
controlled import of plugin manifests). After source changes: regenerate,
then re-verify the handwritten pages against the diff.

## Source-of-truth priority order

1. **Source code**
2. **Tests** (executable specification)
3. **Schema/config** (`database/schema.py`, `config/*.json`, setting schemas)
4. **Generated reference** (`read_only/`, `generated/`)
5. **Handwritten docs** (this knowledgebase, root `README.md`,
   `ARCHITECTURE.md`, `INTEGRATION.md`)

Record any mismatch you find in `DISCREPANCIES.md` — do not silently work
around it. Working rules for agents: [AGENT_GUIDE.md](AGENT_GUIDE.md).

## Index

| Document | What it answers |
|---|---|
| [API_ENDPOINTS.md](API_ENDPOINTS.md) | "What is the exact request/response shape of route X / WS message Y / CLI verb Z / self-command W?" |
| [CONFIGURATION.md](CONFIGURATION.md) | "What can I configure, where, with what defaults/bounds, and does it need a restart?" |
| [CLI.md](CLI.md) | "What commands exist, how are they parsed, how do the REPL and one-shot modes differ?" |
| [DASHBOARD.md](DASHBOARD.md) | "Which routes/WS messages exist, how do I run and secure the dashboard?" |
| [TESTING.md](TESTING.md) | "What does the suite cover, how is it deterministic, how do I run it?" |
| [SECURITY.md](SECURITY.md) | "Where do secrets live, what is gated, what is *not* protected?" |
| [GLOSSARY.md](GLOSSARY.md) | "What does term X mean in this codebase?" |
| [AGENT_GUIDE.md](AGENT_GUIDE.md) | "Where is X, what may I call, how do I debug, what wins in a conflict?" |
| [CHANGELOG.md](CHANGELOG.md) | "What changed, in what order, per the real commits?" |
| [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) | "What is this project?" |
| [ARCHITECTURE.md](ARCHITECTURE.md) | "Why is it shaped this way?" |
| [COMPONENT_MAP.md](COMPONENT_MAP.md) | "Which component owns which responsibility?" |
| [DATA_FLOW.md](DATA_FLOW.md) | "How does a message/policy/task move through the system?" |
| [RUNTIME_LIFECYCLE.md](RUNTIME_LIFECYCLE.md) | "What happens from boot to shutdown?" |
| [ACCOUNT_SYSTEM.md](ACCOUNT_SYSTEM.md) | "How do accounts, caches and supervisors work?" |
| [PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md) | "How does the plugin contract work?" |
| [PLUGIN_API.md](PLUGIN_API.md) | "What may a plugin call?" |
| [PLUGIN_EXAMPLES.md](PLUGIN_EXAMPLES.md) | "Show me working plugin code" |
| [PLUGIN_ANTIPATTERNS.md](PLUGIN_ANTIPATTERNS.md) | "What mistakes break the contract?" |
| [plugins/index.md](plugins/index.md) | "How do I add a plugin? (none ship with the framework)" |
| [EVENT_BUS.md](EVENT_BUS.md) | "Which events exist and how are they dispatched?" |
| [SCHEDULER.md](SCHEDULER.md) | "How is outbound work queued, retried, cancelled?" |
| [RATE_LIMITING.md](RATE_LIMITING.md) | "How do policies, learned defaults, pacing and 429s compose?" |
| [LOGGING.md](LOGGING.md) | "How does structured logging flow (without loops)?" |
| [ERROR_HANDLING.md](ERROR_HANDLING.md) | "Which error means what, and who handles it?" |
| [DATABASE.md](DATABASE.md) | "What is persisted where, and how do migrations run?" |
| [ADAPTERS.md](ADAPTERS.md) | "How is Discord isolated behind one boundary?" |
| [read_only/SYMBOL_INDEX.md](read_only/SYMBOL_INDEX.md) | "Which symbols exist in which module?" (generated) |

Root-level context: `README.md` (project overview + quickstart),
`ARCHITECTURE.md` (design principles), `INTEGRATION.md` (the binding
inter-agent API contract), `TODO + Ideas.md` (the original spec).
