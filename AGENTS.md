# AGENTS.md — LibzentSuite

Binding instructions for AI coding agents in this repository. LibzentSuite is a game-agnostic, multi-account Discord flow-orchestration framework: a framework core, plugins and parser packs, and three interfaces (CLI, FastAPI core API, Flet UI) that share one `ControlAPI` facade.

These rules don't replace the source code, the tests, or the architecture contracts (`docs/architecture/architecture.rst`, `docs/architecture/integration.rst`). Where they disagree with the code, follow §1.6.

Keep the rules high-level. When a detail is needed, read the file that owns it (`pyproject.toml`, the CI workflows, `docs/`) instead of copying it here.

## 1. Working principles

1. **Understand before changing.** Read the implementation, callers, tests, and contracts. Trace behavior across layers. Don't trust stale comments or old plans.
2. **Make the smallest change that fixes the root cause**, with the tests and docs that prove it. No unrelated refactors inside a fix.
3. **Keep `core.api.ControlAPI` as the single facade.** The CLI, API, and UI all call it. Extend it when a feature needs a shared operation; never add a second business-logic path.
4. **Framework → plugins.** The framework provides orchestration, multi-account isolation, and pacing. Plugins drive it live. Plugin cogs receive their account's library client directly through the injected toolbox (owner-locked 2026-11-17). The paced `PluginServices` pipeline remains the rule for coordinated outbound actions. The framework makes no self-initiated Discord API calls.
5. **Never bypass safety.** Don't skip validation, authorization, persistence, scheduling, rate limiting, audit logging, version checks, or server limits to simplify a feature. Pacing is strictest-wins, the safety floor only rises, and server 429s always earn paced retries. Accounts stay isolated unless a workspace couples them on purpose.
6. **Verify, don't assume.** Separate facts, intended behavior, and hypotheses. If requirements conflict in a way that changes product behavior, raise it rather than deciding silently.
7. **Stay within the task.** Change the related source, tests, contracts, and docs. Never rewrite or revert changes you didn't make.
8. **Keep docs in sync** (§5). When a roadmap item completes, move it to the historical record.

## 2. Code quality

- Write for the next reader. Prefer clear control flow, early validation, and small focused functions and modules.
- Name domain concepts. Avoid magic values, cryptic abbreviations, and clever side effects.
- Comments explain *why* (constraints, invariants), never *what*. Remove comments your change made false.
- Leave no dead code, commented-out code, unused imports, or vague TODOs.
- Abstract only for a real shared concept, not for look-alike code.
- Keep dependencies minimal. Prefer the standard library and what the project already uses.

## 3. One source of truth

- Search for an existing owner before adding a helper, model, validator, or component.
- Business rules live in the domain or service layer. Every surface calls them; none re-implements them.
- The backend is authoritative. Frontend validation is for feedback only.
- Keep one canonical copy of each piece of persisted state and each data contract. Link to contracts; don't copy them into other docs.
- Don't merge flows that only look alike.

## 4. Types and data

- The baseline is **Python 3.10**. Annotate new and changed code fully.
- Prefer precise types to `Any`. Contain `Any` at dynamic boundaries and validate before data enters the domain.
- Treat external input (JSON, HTTP payloads, env values, plugin manifests) as untrusted until validated. A `cast` is not validation.
- Model absence as `T | None`. Use dataclasses, enums, or `TypedDict` for stable shapes. Keep plain dicts at serialization boundaries.
- Keep exception handling narrow. Never swallow `asyncio.CancelledError`. Don't block the event loop.

## 5. Documentation and knowledgebase

- **Agent rules and binding contracts** live only in `AGENTS.md` and `docs/architecture/`. Don't put coding rules, agent instructions, or lint policy in `docs/` or `knowledgebase/`.
- **`docs/`** is the user-facing guide: installation, running the suite from the CLI and the UI, environment variables, commands, and plugin-author references. Keep it current with every feature or command change. It describes the code; when they disagree, the code wins.
- **Platform note:** the project is platform-independent — no platform-only modules, and one suite that runs green on Linux and native Windows. CI runs it on Linux; the local gate covers Windows; keep both statements true.
- **`knowledgebase/`** is local-only and append-only. Don't rewrite existing entries. Correct an earlier statement with a dated addendum. Record facts and decisions, never new rules.
- **`dump/`** holds retired code. Leave it alone.
- Public classes, functions, and subsystems get a docstring covering their contract. Add an append-only knowledgebase reference for them.
- Never commit secrets, tokens, personal data, or live credentials to docs, fixtures, logs, or examples. Use synthetic values.

## 6. Interfaces and UI

- Layout, navigation, and interaction can change without changing backend behavior. The current Flet UI design contract is `docs/plans/flet_ui_design_contract.md`.
- Every component needs defined states (loading, disabled, success, error), validation, keyboard and focus behavior, responsive behavior, and accessible names.
- Use theme tokens, not hard-coded colors, sizes, or fonts.
- Plugins never get direct access to UI or backend internals.
- Import `flet` lazily so the core and tests run without the GUI installed.

## 7. Testing

- Every behavior change gets tests of its observable contract and of the regression that motivated it.
- Tests are deterministic and offline: injected clocks, seeded randomness, in-memory or simulated Discord, no real network, no live accounts, no wall-clock sleeps.
- **Never touch the real `state/` directory** from tests or scripts. It holds the live databases and the credentials key. Tests use disposable storage and clean up only what their own run created.
- Cover failure paths, cancellation, duplicate calls, and races. For stateful operations, assert both the result and what was persisted.
- Run narrow tests first, then the adjacent suites, then the full suite. CI runs Python 3.10–3.13 and also runs the suite on Windows, so check 3.10 for changes touching dependencies, typing, or stdlib APIs, and keep stdlib use portable.
- Never weaken a test to get green. Fix the cause, or document a pre-existing failure with evidence.
- Coverage measures production code only. The coverage floor only rises.
- Report exactly what you ran and what you didn't. Never claim a check passed unless it did.

## 8. Lint, types, and dependencies

- **Ruff** owns linting, formatting, and import order. Don't add a competing formatter.
- **pyright** is the static type checker (adopted 2026-11-19) and is a ratchet gate with `.github/pyright-baseline`. CI fails when the error count grows, and also when it shrinks without the baseline file being lowered in the same PR. Never raise the baseline. Use narrow, reasoned suppressions only.
- The canonical configuration is `pyproject.toml`. Read it rather than copying settings.
- Dependencies are declared in `pyproject.toml`. `uv.lock` pins them, and `requirements.txt` is generated from the lock. Regenerate it; never hand-edit it. Dev-only tools and test-only dependencies stay out of runtime dependencies.
- The live Discord client (`better.py-self`) is imported lazily, only at the `adapters/discord/` boundary.
- Pin every CI action and record it in `.github/workflows/actions.lock`. Workflow tokens stay read-only.
- Verify third-party API behavior against current docs and the installed version, not memory.

## 9. Change checklist

- [ ] Read the relevant code and tests.
- [ ] Respected module ownership and contracts, or updated the contract on purpose.
- [ ] Reused the existing owner of any shared logic.
- [ ] Added types, docstrings, and regression tests, including failure paths.
- [ ] Synced `README.md`, `docs/`, `docs/roadmap.md`, and `CHANGELOG.md` `[Unreleased]` where affected.
- [ ] Ran Ruff, pyright, and the relevant tests. Recorded anything not run.
- [ ] Reviewed the full diff for unrelated changes, stale comments, API breakage, secrets, and dead code.

## 10. Commits and pushing

- Work on a **feature branch**. Confirm it with `git branch --show-current` before the first edit. Never commit to `master`, `main`, or `dev`.
- Stop any running service that hosts the changed code before committing, so the committed state is the tested state.
- A red suite blocks the commit. Fix it first.
- Review `git diff` and `git status` before each commit. Never commit a file you can't describe.
- One logical change per commit, in dependency order: foundation → feature → tests → docs.
- Commit messages: `type(scope): what changed — and why it matters to users`. Name the trigger (bug report, audit finding, CI failure) when relevant.
- Never bypass hooks with `--no-verify`. If a hook rewrites files, re-stage and commit again.
- **Never** run `git push`, `git reset`, `git clean`, or switch branches without explicit authorization in the same request.
- **Never** create releases or `v*` tags. The tag ruleset is locked deliberately.
- Push only with the owner's explicit approval, and only when at least five commits are pending.
- Before any approved push: run the full local gate (`scripts/local-ci.sh`) until it is green, then run the CodeRabbit review over the pending range (see the `coderabbit-code-review` skill). Both are required. Neither replaces the other.
- The local gate must never delete or overwrite `state/`. If a script would, stop and report it.
- Leave the working tree clean. Say what you deliberately left uncommitted, and why.

## 11. Naming

- Classes are nouns that name a domain concept (`PascalCase`). Functions are verbs (`snake_case`). Booleans read as questions (`is_ready`, `can_retry`).
- Use the project's existing vocabulary: workspace, chain, signal, preset, profile, pacing. Don't invent synonyms.
- Names should be expressive and memorable where that aids understanding, but never misleading or cryptic.

## 12. Skills

- Local skills live in `.agents/` (gitignored). When a skill is available, use it. Otherwise fall back to the built-in tools.
- **fast-edit:** load it before any file edit or write, and use its commands instead of the built-in edit tools.
