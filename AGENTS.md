# AGENTS.md — LibzentSuite Engineering Standards

Binding instructions for AI coding agents working anywhere in this repository. LibzentSuite is a game-agnostic, multi-account Discord flow-orchestration framework: a framework core, pluggable plugins and parser packs, and three interfaces — CLI, FastAPI core API, Flet UI — that all sit on one `ControlAPI` facade.

These rules do not replace the current source code, the tests, or the architecture contracts (`docs/architecture/architecture.rst`, `docs/architecture/integration.rst`). Where they disagree with the code, follow §1.6.

## Quick reference

CI runs every check through `uv run --frozen`, so the lock pins the toolchain.

```bash
uv sync --frozen                                               # install the locked environment (runtime + dev group)
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen mypy                                           # strict typing, zero-error floor (§8)
uv run --frozen python -m unittest discover -s tests -t . -v   # offline suite: no network, no live accounts
bash scripts/local-ci.sh                                       # mirrors CI; quick mode to iterate, full mode before any push (§12)
python3 main.py --sim pacing_respected                         # whole stack over the offline Discord simulator
```

| Path | Role |
| --- | --- |
| `core/` | Framework: `api.py` (`ControlAPI`), `bootstrap.py` (composition root), `plugin_base.py` (plugin contract), and the managers for accounts, workspaces, plugins, rate limiting, task scheduling, and events |
| `adapters/` | Discord boundary: `discord/` (live client, imported lazily) and `simulator/` (complete offline world) |
| `database/` | SQLite models, ordered migrations, repositories |
| `interfaces/` | `cli/` (REPL + one-shot), `api/` (FastAPI), `ui/` (Flet, imported lazily) |
| `plugins/`, `parsers/` | Runtime-discovered plugins (the framework bundles none); the parser registry — game-specific logic lives in parser packs, never in `core/` |
| `tests/`, `benchmark/`, `scripts/`, `config/` | `unit`/`integration`/`simulation`/`stress` suites; benchmarks; tooling; configuration |
| `docs/`, `knowledgebase/`, `dump/` | See §5 |

`README.md` → Reference → Layout has more detail. Verify any path against the tree before relying on it.

## 1. Working principles

1. **Understand before changing.** Read the relevant implementation, callers, tests, data models, and contracts; trace behavior end to end when it crosses layers. Don't infer current behavior from stale comments or old plans.
2. **Solve the requested problem completely — and no more.** Make the smallest coherent change that fixes the root cause, with the tests and docs that make it understandable and verifiable. No unrelated refactors inside a fix.
3. **Preserve architecture boundaries.** `core.api.ControlAPI` is the one application facade for the CLI, the FastAPI core API, and the Flet UI. Extend it (and its tests) when a feature legitimately needs a shared operation; never add a second business-logic path to avoid touching it.
4. **Keep plugins modular — Core Engine (framework) → Driver (plugins).** The framework provides capabilities; plugins use them live through `PluginServices`. Plugins never touch Discord clients directly: the framework services are the authoritative middleware, for security. The framework itself makes no self-initiated Discord API calls — every call is a plugin action through the paced pipeline.
5. **Preserve safety and data semantics.** Never bypass validation, authorization, persistence, task scheduling, rate limiting, audit logging, generation/version checks, or server-provided limits to simplify a feature or its UI. Outbound actions flow `PluginServices` → `TaskScheduler` → `RateLimitManager` → `AccountManager.send_hook` → Discord client: the strictest pacing wins, the safety floor only rises, and server 429s always earn paced retries. Accounts stay isolated unless a workspace deliberately couples them (shared pacing clock, chains).
6. **Prefer verified behavior to assumptions.** Separate confirmed facts, intended behavior, and hypotheses. When requirements conflict materially, surface the conflict instead of silently inventing a product decision; resolve minor technical details yourself when they change neither product behavior nor a public contract.
7. **Respect the task boundary, not an arbitrary file list.** A cross-cutting fix may need coordinated updates to source, tests, contracts, and docs. Change the smallest set of related files, preserve unrelated work, and never rewrite unrelated user changes.
8. **Keep docs synced.** Update what the change affects — `README.md`, `docs/`, `docs/roadmap.md`, `CHANGELOG.md` (`[Unreleased]`) — per §5. When a roadmap item completes, move it out of the roadmap into the historical record: `docs/reference/completed.rst` plus a knowledgebase addendum.

## 2. Readability and maintainability

- **Optimize for the next reader**, not for line count or cleverness. Prefer explicit control flow, early validation, clear error paths, flat conditionals (no chained ternaries or clever side effects), and small cohesive functions and modules. Handle expected failures close to the boundary and keep diagnostic context.
- **Name domain concepts instead of using magic values.** Use named constants, enums, or typed models where they clarify an invariant; don't add constants or wrapper layers that only obscure an obvious value.
- **Comments explain why** — a constraint, an invariant, a non-obvious choice — never what the next line does. Fix or delete comments your change made false. House style: docstrings and comments may use typographic punctuation (— → ×); don't flatten it to ASCII.
- **Leave nothing behind:** no dead code, commented-out code, unused imports, abandoned compatibility paths, or TODOs that merely restate unfinished work. A TODO states what remains and why it is out of scope.
- **Abstract for a real concept** — a shared domain idea, several consumers, a meaningful contract, or a clear extension point — not because two fragments look alike. Extract a helper when it names a real operation or untangles complex logic; avoid both copy-pasted behavior and abstractions that only remove a few harmless repeated lines.

## 3. Avoid duplication and keep one source of truth

- **Search before you add.** Look for an existing owner of the responsibility — model, validator, serializer, error type, formatter, API method, event type, repository, UI component — before creating a helper or feature.
- **Business rules live in their owning domain/service layer.** The CLI, API, UI, plugins, repositories, and test fixtures call them; they never re-implement them. Client-side validation is feedback only — the backend validates again and stays authoritative.
- **Keep one canonical representation of persisted state.** No redundant caches or duplicate mutable fields unless ownership, invalidation, and consistency rules are explicit.
- **Shared components and helpers need a clear contract and state model** — no universal abstraction with dozens of unrelated flags.
- **Put each contract at its natural owner and link to it** instead of re-copying it into handwritten docs. When a contract changes, update every call site and regression test.
- **Keep meaningful differences.** Don't merge flows that merely look alike; first establish that they share semantics.

## 4. Python types and data models

Supported baseline: **Python 3.10+**. New or materially changed code is fully annotated; mypy strict (§8) enforces the mechanics, so this section covers what the checker cannot.

- **Prefer precise types over `Any`.** Use `object`, `Mapping`, `Sequence`, `Iterable`, `Protocol`, `TypedDict`, `Literal`, unions, and generics where they express the contract. `Any` belongs only at genuinely dynamic boundaries — contain it and validate before data reaches the domain layer.
- **Model absence honestly** as `T | None`; narrow with explicit checks instead of a false default.
- **External input is untrusted until validated** — decoded JSON, HTTP payloads, environment values, plugin manifests. A `cast` is never validation.
- **Give stable shapes a real model** (dataclass, `Enum`, `TypedDict`) instead of an untyped dict. Keep plain dicts at serialization and genuinely dynamic boundaries, and convert explicitly there (`as_dict`, serializers, DTO conversion). Use `Enum` for closed sets of domain states; `StrEnum` is 3.11+, so don't use it on the 3.10 baseline.

### Dataclasses

Use `@dataclass` when a class mainly represents structured data with named fields and predictable construction and equality.

- Annotate every field. Use `frozen=True` for value objects, event payloads, configuration records, and DTOs unless mutation is part of the contract. Keep genuinely mutable runtime/lifecycle state mutable — don't freeze a manager just to follow a style rule.
- Use `field(default_factory=...)` for mutable defaults; never share a mutable default between instances.
- Consider `slots=True` for small, high-volume carriers and `kw_only=True` for models with many or optional fields — after checking compatibility; don't change a stable public constructor casually.
- Validate in `__post_init__` only for invariants local to the value object. IO, repository access, network calls, and workflows belong in service methods.
- Don't keep a dataclass and a parallel dictionary for the same data: one canonical in-memory representation, converted explicitly at boundaries.

### Exceptions and async

- Raise the project's domain-specific exceptions; keep messages human-readable and chain with `raise ... from exc` where it adds diagnostic value.
- Catch only what the code can meaningfully handle. Broad `except Exception` is acceptable only at a documented isolation boundary that logs the error and keeps the right scope alive.
- Never swallow `asyncio.CancelledError` or turn cancellation into success. Use `try/finally` for cleanup and keep task and resource ownership clear.
- Don't block the event loop with synchronous IO or long CPU work; preserve the existing scheduler, persistence, and lifecycle ownership boundaries.

## 5. Documentation and knowledgebase policy

| Location | Role | Rules |
| --- | --- | --- |
| `AGENTS.md`, `docs/architecture/architecture.rst`, `docs/architecture/integration.rst` | Agent guidance and the binding engineering/architecture contracts | The only places for them. Verify against the code; fix or report stale contracts. |
| `docs/design/`, `docs/plans/` | Subsystem design specs and plans: locked decisions and intended direction (workspace design, data-management adoption, the Flet UI design contract, …) | Read the one for the subsystem you touch. If code and spec disagree, surface it (§1.6) instead of picking silently. |
| rest of `docs/`, `README.md`, `CHANGELOG.md` | User-facing description, the roadmap (`docs/roadmap.md`), and history | Descriptive only — no coding rules, agent instructions, linting policy, or behavioral mandates. |
| `knowledgebase/` | Technical reference and project history | Local-only (gitignored, absent from a fresh clone). Append-only. No rules. |
| `dump/` | Retired code, preserved verbatim | Local-only; only `dump/README.md` is tracked. Leave it as is. |

### `docs/`

- Describe the project's structure and how to use every available command and feature, in user-readable form; future plans and design direction live here too.
- Docs are a copy of what the codebase does, not the source of truth. When docs and code disagree, the code wins and the docs get fixed.
- Any feature or command added, removed, or changed updates the related docs in the same change — never leave stale dumps.
- The core audience is plugin authors: document the `Plugin` and API surfaces (endpoints, settings, and functions available to plugins) well enough to build a plugin from the docs alone.
- Give complete working knowledge of running the suite from every supported interface (CLI, API, UI): installation through a running suite, including the environment variables and data to fill in first.
- State the runtime-environment note explicitly: the project was built and tested on Linux only and other platforms are untested. Change that note only when the full suite runs green elsewhere.

### `knowledgebase/`

- Treat existing entries as read-only history in ordinary work: don't rewrite, delete, reorder, or "clean up" them.
- It is append-only: add a new focused document or a dated, attributed addendum. Correct an earlier statement with a traceable correction that says what it supersedes — never replace the original.
- Record facts, contracts, architecture, observed behavior, decisions already made, examples, caveats, and source links — never new rules. A decision that must bind goes into `AGENTS.md` or the architecture contracts.
- When you add or materially change a public class, function, method, data model, or subsystem, document its purpose and contract in the docstring (inputs, outputs, exceptions, side effects, lifecycle/ownership, persistence, invariants, callers) and add or update a knowledgebase reference the append-only way. The docstring stays next to the code; the knowledgebase holds cross-module understanding.
- If generated knowledgebase areas are ever restored, follow the generator's ownership: never hand-edit generated output; regenerate after source changes and review the diff. Don't claim knowledgebase tooling or protections exist unless verified.

**Never** put secrets, tokens, personal data, or live account credentials in docs, fixtures, examples, logs, or knowledgebase entries — use synthetic identifiers and redacted examples. (gitleaks runs in pre-commit and CI.)

## 6. Public contracts, architecture, and UI

- Read `docs/architecture/architecture.rst` (ownership and boundaries) and `docs/architecture/integration.rst` (the current inter-module contract) first. When they look stale, verify them against the code, don't perpetuate a known contradiction, and propose the correction.
- `core.api.ControlAPI` stays the shared facade for the CLI, the FastAPI core API (`interfaces/api/`), and the Flet UI (`interfaces/ui/`). When a feature needs a new shared operation, add it there and update the implementation, every interface, and the tests together.
- UI work follows the owner's Flet UI design contract, `docs/plans/flet_ui_design_contract.md`. Layout, navigation, component structure, and interaction flow may change without changing backend behavior; a previous page list or implementation is not a standing requirement unless the task or a current contract reaffirms it.
- Specify each component: purpose, inputs/outputs, interaction states (disabled, loading, success, failure), validation, focus/keyboard behavior, responsive behavior, and accessible name/state. Judge acceptance by observable behavior and user outcomes, not by a markup trick or the presence of a comment.
- Prefer theme tokens and existing theme-aware values over hard-coded colors, dimensions, and fonts; components must stay usable across supported themes and density settings.
- Keep framework-owned views and plugin-authored views within their established boundaries: plugins get no direct access to UI internals, backend internals, or global application state.
- `flet` is an optional dependency, imported lazily, and absent from the dev/CI environment. Keep it out of module-level imports on any path the tests exercise, so the API-client core and the suite run without the GUI installed.
- Never fix a frontend problem by bypassing backend validation, authorization, transaction semantics, state transitions, or persistence guarantees.

## 7. Testing and regression prevention

- **Every meaningful behavior change gets tests** of the externally observable contract and of the regression that motivated it — not of implementation text or helper names.
- **Deterministic and offline.** Use injected clocks, seeded randomness, in-memory repositories/databases, the simulator adapter, explicit synchronization, and bounded waits. No wall-clock sleeps, real network access, live Discord accounts, or real tokens in ordinary tests.
- **Isolated storage — never the real `state/`.** Tests use disposable databases, key material, and directories. `state/` holds the live databases and the credentials key: never delete, overwrite, or "reset" it, and never run a script that does. Starting each run from a fresh state means removing only the leftovers the previous test run itself produced (temp data, caches).
- **Cover the edges:** happy path, invalid input, missing/unknown data, boundary values, failure recovery, cancellation, duplicate calls, and relevant races. For stateful operations assert both the result and the persisted state.
- **Test at the boundary.** For API or model changes, test serialization and compatibility. For UI changes, test interaction states, stale/loading/error flows, keyboard behavior, responsive layout where tooling supports it, and backend persistence.
- **Run narrow first**, then the adjacent unit/integration tests, then the full suite before claiming repository-wide correctness. CI runs Python 3.10–3.13: verify changes that touch dependencies, typing, or stdlib APIs on 3.10, the minimum, not only on your local interpreter. If something couldn't be run, say exactly what was and wasn't run.
- **Never weaken tests** — no deleted assertions, skips, or loosened validation to get green. Fix the cause, or document a genuinely pre-existing failure with reproducible evidence.
- **Coverage measures production code only** (never add `tests/` to coverage `source`). The floor (`fail_under` in `pyproject.toml`) only rises; never lower it without recording why.

## 8. Formatting, linting, and type checking

**Ruff** owns linting, formatting, and import sorting; **mypy** owns strict static typing. The one canonical configuration is the root `pyproject.toml` — read it instead of restating it here, and never add a competing Black/isort/flake8 setup. Tool versions are pinned in `[dependency-groups] dev`; when upgrading, check the tools' current official documentation (mypy's strict flags are version-sensitive).

**Toolchain.** Dependencies live in `[project]`, dev tools in `[dependency-groups] dev`, and `uv.lock` is the pinned universal resolution. CI installs it with `uv sync --frozen` and runs every check through `uv run --frozen`. `requirements.txt` is a generated pip-compat export of the lock — regenerate it, never hand-edit:

```bash
uv export --format requirements-txt --no-hashes --no-dev --no-emit-project -o requirements.txt
```

CI's export-sync and lock-integrity gates enforce this.

**mypy strict is a hard floor.** Two equality-ratchet gates — `.github/mypy-baseline` (production) and `.github/mypy-tests-baseline` (tests) — both sit at zero, so any new strict error fails CI. A count that changes without its baseline file changing in the same PR also fails. Never raise a baseline, and never add a blanket ignore or suppression to move a count.

**Ruff practice**

- CI runs `ruff check` and `ruff format --check`; run `ruff format` locally on the files you change. The pre-commit hooks (`ruff --fix`, `ruff-format`, gitleaks, whitespace/EOF fixers) are a fast subset of CI. They may rewrite files — re-stage and re-commit — and must never be bypassed (`--no-verify`).
- Inspect every auto-fix diff; never auto-fix blindly across unrelated files.
- No unstable/preview rules by default. Add new rule families intentionally, and fix or narrowly justify the exceptions.
- Ignore a rule only with a concrete reason and the narrowest scope — never whole categories or modules to hide debt.

**Typing practice**

- Resolve type errors by improving the model or the boundary validation first. Avoid `Any`, `cast`, `type: ignore`, `# noqa`, and unchecked deserialization as shortcuts.
- Every suppression uses a specific error code, says why the checker cannot express the invariant, and is as local as possible. Remove obsolete ones when the code changes.
- Never set `ignore_missing_imports = true` globally. A narrow per-module exception is acceptable only when a dependency lacks usable typing and a stub/adapter isn't justified — the existing `flet` override is the model.
- Keep `warn_unreachable` and `warn_unused_configs` on. Use `reveal_type()` only temporarily; never commit it.

Official references: [Ruff configuration](https://docs.astral.sh/ruff/configuration/), [Ruff formatter](https://docs.astral.sh/ruff/formatter/), [mypy configuration](https://mypy.readthedocs.io/en/stable/config_file.html).

## 9. Dependency and compatibility discipline

- Preserve the declared Python minimum (3.10) and the runtime-dependency policy. Avoid APIs newer than 3.10 unless the minimum is deliberately raised with CI and docs updated.
- A new dependency needs a clear need and a compatibility review; prefer the standard library and dependencies already approved. Keep the core runtime set minimal: optional interface dependencies (API/UI) belong in extras, and development/CI-only tools in `[dependency-groups] dev`. Audit declared dependencies against actual imports, including test-tooling paths (`fastapi.testclient` needs `httpx` on 3.10).
- When dependencies change: edit `pyproject.toml`, refresh `uv.lock`, regenerate `requirements.txt` (§8), and keep the CI audit gates (pip-audit, bandit) green.
- The live Discord client (the `better.py-self` fork) is imported lazily and only at the `adapters/discord/` boundary; simulations and tests never need it or a network.
- CI actions are dependencies too: pin every action and record it in `.github/workflows/actions.lock` (machine-generated by `gh actions-lock`, validated weekly by the health sweep); workflow tokens stay read-only.
- Centralize compatibility logic behind explicit protocol/adapter boundaries instead of spreading version checks.
- For third-party behavior that may have changed, check the current official documentation and the installed version — not memory.

## 10. Change completion checklist

Before declaring a change complete:

- [ ] Read the relevant implementation, callers, and existing tests (§1).
- [ ] Respected module ownership and existing contracts — or updated the contract deliberately (§1, §6).
- [ ] Reused the canonical owner instead of duplicating logic (§3).
- [ ] Added complete types, useful docstrings, and a data model that fits the data (§4).
- [ ] Added regression tests for the behavior and its important failure paths (§7).
- [ ] Synced `README.md`, `docs/`, `docs/roadmap.md`, and `CHANGELOG.md` as affected, and added an append-only knowledgebase reference — with no rules added to docs or the knowledgebase (§5).
- [ ] Ran Ruff, mypy, and the relevant tests (and `scripts/local-ci.sh` before any push); recorded anything not run. Never imply a command succeeded unless it actually ran (§7, §8, §12).
- [ ] Reviewed the complete diff for unrelated changes, stale comments, accidental API breakage, credentials, and dead code.

## 11. Naming Conventions

Names should be **expressive, informative, and fitting to the project's domain** — memorable where that helps understanding, never cryptic, misleading, or clever at the expense of clarity. Reuse the project's existing vocabulary (workspace, chain, signal, preset, profile, pacing, …) instead of coining synonyms, and keep terminology consistent across modules.

- **Classes and dataclasses:** nouns or domain concepts that state identity and responsibility (`PascalCase`).
- **Functions and methods:** action-oriented names that say what they accomplish (`snake_case`).
- **Variables and attributes:** names that convey intent, context, and expected values.
- **Booleans:** read as a question — `is_ready`, `has_access`, `can_retry`.

A good name lets the next developer understand the code without reverse-engineering its intent.

## 12. Commit Discipline

Commit discipline lives here; there is no separate file for it.

**Before committing**

- Work on a **feature branch** and confirm it with `git branch --show-current` before the first edit. Never commit on `master`, `main`, or `dev`. If you are not on a feature branch, stop and ask — switching branches needs explicit authorization.
- **Stop running services** that host the changed code (API/UI servers, bots) so the committed state is the tested state.
- Run the relevant test suites and record the outcome. **A red suite blocks the commit series** — fix first, then commit.
- Review the full diff (`git diff` + `git status --porcelain`). Never commit a file you cannot describe.
- Hooks (§8) may rewrite files: re-stage and re-commit. Never bypass them with `--no-verify`.

**Granularity — one logical unit per commit**

- **One file = one commit** when the file carries its own concern (13 files → 13 commits, each describing that file's changes).
- A cohesive new feature may group its own package files into one PR, with its tests and docs as separate follow-up commits in that PR.
- Commit in **dependency order**: foundation → feature → tests → docs.

**Messages**

- Format: `type(scope): what changed — and the user-visible why`, for example `feat(parsers): …`, `fix(ui): …`, `test(workspace): …`, `docs: …`, `chore(deps): …`, `fix(ci): …`.
- Describe the changes in the committed files specifically — not a vague round summary. Name the trigger (user report, audit finding, ticket, CI failure) in the body when it helps.

**Scope and safety**

- **Never** `git reset`, `git clean`, or switch branches unless explicitly authorized in the same breath as the work.
- **Never** discard unrelated user changes. Unrelated modified files stay uncommitted (or are committed separately with an accurate message if the user asked for "all changes").
- Never create release tags or releases: the `v*` tag ruleset is locked deliberately until the owner lifts the hold.
- Commit only after the final diff review. The working tree ends **clean** — everything relevant committed, nothing extra.

**Pushing**

- A push needs the owner's explicit approval **and** five or more unpushed commits. **Never** push without both.
- Before **every** approved push: run the full local gate (`bash scripts/local-ci.sh`, full mode) until it is green, then run the CodeRabbit CLI review over the pending range (the `coderabbit-code-review` skill documents the workflow) and fix or triage its findings. The CI gates and the pre-review are both mandatory — neither substitutes for the other.
- The local gate must never touch `state/` (§7). If `scripts/local-ci.sh`, or any helper, would delete or overwrite it, stop and report instead of running it.

**After the series**

- Show `git log --oneline` for the new commits and the final `git status`.
- State what was intentionally left uncommitted and why.

## 13. Skills & Tools

Local agent skills live in `.agents/` (gitignored: per-machine, absent from a fresh clone). Use them when present; if a skill isn't installed, fall back to the built-in tools instead of stopping.

**fast-edit** — for every edit, create, write, or save of a file:

1. Load it first: `skill("fast-edit")`.
2. Use its commands (`show`, `replace`, `insert`, `delete`, `batch`, `paste`, `write`, `generate`) instead of the built-in Edit/Write tools.
3. Prefer `fast-batch` and `fast-write` for batch edits and multi-file writes, and `save-pasted` (zero token, zero escaping) for user-pasted content.

**coderabbit-code-review** — the pre-push review workflow (§12).
