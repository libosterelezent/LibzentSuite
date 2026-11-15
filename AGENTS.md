# AGENTS.md — LibzentSuite Engineering Standards

These instructions apply to the whole repository. They describe how to make changes that are correct, readable, maintainable, and consistent with LibzentSuite's architecture. They are not a substitute for the current source code, tests, `docs/architecture/architecture.md`, or `docs/architecture/integration.md`.

## 1. Working principles

1. **Understand before changing.** Read the relevant implementation, callers, tests, data models, and current architecture/integration contracts before editing. Trace a behavior end to end when it crosses layers. Do not infer current behavior from stale comments or old plans alone.
2. **Solve the requested problem completely.** Make the smallest coherent change that fixes the root cause. Include the tests and documentation needed to make the change understandable and verifiable. Do not make unrelated refactors while fixing a defect.
3. **Preserve architecture boundaries.** `core.api.ControlAPI` is the shared application facade for the CLI and dashboard. Both interfaces must use the same application behavior. Extend the shared facade and its tests when a legitimate feature requires it; do not add a second business-logic path to avoid modifying the API. (this api facade will be transformed into a full API with FastAPI.)
4. **Keep plugins Modular.** Plugins uses the provided services and methods from the framework, and therefore cannot directly control and have access to any client, our provided services are the middleware and is authoratative for security reasons, the most descriptic way to describe the structure is: Core Engine (framework) -> Driver (Plugins) in which case core engine provides the ability and plugins utilize them live.
5. **Preserve safety and data semantics.** Do not bypass validation, authorization, persistence, task scheduling, rate limiting, audit logging, generation/version checks, or server-provided limits in order to simplify a feature or its UI.
6. **Prefer verified behavior to assumptions.** Distinguish confirmed facts, intended behavior, and hypotheses. When requirements conflict materially, surface the conflict instead of silently inventing a product decision. Resolve minor technical details independently when they do not change product behavior or public contracts.
7. **Respect the task boundary, not an arbitrary file list.** A cross-cutting fix may need coordinated updates to source, tests, contracts, and documentation. Change the smallest set of related files needed; preserve unrelated work and never rewrite unrelated user changes.
8. **Keep docs always updated and synced.** Reflect the changes that happens in project, evaluate the type of change and respectively update the most relative file/folder the change is subjected to, be either README.md, ROADMAP.md files and `docs/`, `knowledgebase/` folders, once any roadmap item is marked completed migrate it into knowledgebase and docs as a historical record.

## 2. Readability and maintainability

- Optimize for the next developer understanding the code, not for minimum line count or cleverness.
- Use descriptive names that express domain meaning. Avoid opaque abbreviations, unnecessary single-letter names, deeply nested conditionals, chained ternaries, clever side effects, and functions that perform unrelated jobs.
- Keep functions and methods focused. Extract a helper when it names a real reusable operation or makes complex logic easier to follow. Avoid both copy-pasted behavior and abstractions created solely to eliminate a few harmless repeated lines.
- Prefer explicit control flow, early validation, clear error paths, and small cohesive modules. Handle expected failures close to the boundary and preserve useful diagnostic context.
- Avoid magic numbers and string literals for domain concepts. Use named constants, enums, or typed models when they clarify invariants. Do not create constants or wrapper layers that only obscure an obvious value.
- Comments should explain **why** a constraint exists, what invariant must be preserved, or why a non-obvious choice is necessary. Do not narrate each line of straightforward code. Remove or update comments made false by the change.
- Do not leave dead code, commented-out implementations, unused imports, abandoned compatibility paths, or TODOs that merely restate unfinished work. A TODO should explain the remaining work and its reason when the work cannot be completed in scope.
- Avoid premature generalization. Introduce an abstraction when there is a real shared concept, multiple consumers, a meaningful contract, or a clear extension point—not merely because two code fragments look similar.
- Avoid unnecessary dependencies. Prefer the standard library and dependencies already approved by the project; new runtime dependencies require a clear need and compatibility review.

## 3. Avoid duplication and keep one source of truth

- Before adding a helper or feature, search for existing models, validators, serializers, error types, formatting helpers, API methods, event types, repositories, and UI components that already own the responsibility.
- Keep business rules in their owning domain/service layer. Do not duplicate the same rule independently in the CLI, dashboard, plugin, repository, or test fixture.
- Keep a single canonical implementation of shared behavior. Call it from each surface instead of copying its logic. A frontend may provide user-friendly validation for immediate feedback, but the backend remains authoritative and must validate again.
- Use shared components for repeated UI behavior, and shared API/client helpers for common transport behavior. A shared component must have a clear contract and state model; do not build a universal abstraction with dozens of unrelated flags.
- Keep one canonical representation of persisted state. Do not maintain redundant caches or duplicate mutable fields unless ownership, invalidation, and consistency rules are explicit.
- Do not duplicate data contracts in multiple handwritten documents. Put the contract at its natural owner, then link/reference it from related documentation. Update all dependent call sites and regression tests when the contract changes.
- Preserve meaningful differences between similar flows. Do not merge distinct domain behavior merely because the UI looks alike; first establish whether they truly share semantics.

## 4. Python types and data models

The project supports **Python 3.10 or newer**. New or materially changed Python code should use complete, useful type annotations.

- Annotate public functions, methods, constructors with meaningful return types, module-level APIs, callbacks, async functions, and non-obvious local variables. Use `-> None` for procedures that return no useful value.
- Prefer precise types over `Any`. Use `object`, `Mapping`, `Sequence`, `Iterable`, `Protocol`, `TypedDict`, `Literal`, unions, generics, and domain-specific types where they express the contract better. `Any` may be appropriate at genuinely dynamic boundaries, but contain it and validate before passing data into the domain layer.
- Model nullable values honestly as `T | None`; do not use a false default to avoid handling absence. Narrow values with validation and explicit checks before use.
- Treat decoded JSON, HTTP payloads, environment values, plugin manifests, and other external input as untrusted until validated. Do not use a cast as a substitute for runtime validation.
- Avoid untyped dictionaries as internal domain models when the data has a stable shape. Use a dataclass, enum, `TypedDict`, or another explicit model where it improves correctness and readability. Keep plain dictionaries at serialization or genuinely dynamic-map boundaries.
- Use `Enum`/`StrEnum` (the latter is available in Python 3.11, so do not require it for the Python 3.10 baseline) for closed sets of domain states when it improves validation and clarity.

### Dataclasses

Use `@dataclass` when a class primarily represents structured data with named fields and predictable construction/equality, rather than hand-written boilerplate.

- Annotate every field. Use `frozen=True` for immutable value objects, event payloads, configuration records, and DTOs when mutation is not part of the contract.
- Consider `slots=True` for small, high-volume data carriers when compatibility and the need for dynamic attributes have been checked.
- Use `kw_only=True` when keyword-only construction materially reduces mistakes in a model with many fields or optional values; do not change a stable public constructor casually.
- Use `field(default_factory=...)` for mutable defaults. Never share a mutable list/dict/set default between instances.
- Keep mutable runtime/lifecycle state mutable when that is genuinely required; do not freeze a manager or runtime object merely to follow a style rule.
- Add validation in `__post_init__` only when the invariant belongs to the value object and can be checked locally. Put IO, repository access, network calls, and complicated workflows in service methods, not dataclass constructors.
- Avoid maintaining both a dataclass and a parallel dictionary containing the same information. Convert explicitly at boundaries (`as_dict`, serializers, API DTO conversion) and keep one canonical in-memory representation.

### Exceptions and async code

- Raise domain-specific exceptions already owned by the project where appropriate. Preserve human-readable messages and exception chaining (`raise ... from exc`) where it adds diagnostic value.
- Catch only exceptions the code can meaningfully handle. Avoid broad `except Exception` unless it is a documented isolation boundary that logs the error and keeps the correct scope alive.
- Do not swallow cancellation (`asyncio.CancelledError`) or turn cancellation into success. Use `try/finally` for cleanup and make task/resource ownership clear.
- Do not block the event loop with synchronous IO or long CPU work. Preserve the existing scheduler, persistence, and lifecycle ownership boundaries.

## 5. Documentation and knowledgebase policy

The root `AGENTS.md` plus `docs/architecture/architecture.md` and `docs/architecture/integration.md` are the places for agent guidance and binding engineering/architecture contracts. **Do not put coding rules, agent instructions, linting policy, or behavioral mandates inside `knowledgebase/` or `docs/`.**

`knowledgebase/` exists to preserve project knowledge and technical reference material, not to govern agents.

- Treat existing knowledgebase entries as read-only historical knowledge during ordinary implementation work. Do not rewrite, delete, reorder, or silently “clean up” existing material.
- The knowledgebase is append-only: add knowledge as a new focused document or a clearly dated/attributed addendum. Never replace earlier observations; correct a previous statement by adding a traceable correction that explains what superseded it.
- Do not turn knowledge entries into new rules. Describe facts, contracts, architecture, observed behavior, decisions already made, examples, caveats, and links to the source. Decisions that need to become binding instructions belong in the root-level contract documents instead.
- When adding or materially changing a public class, function, method, data model, or subsystem, document its purpose and relevant contract in the code docstring and add/update a knowledgebase reference using the append-only approach. Include applicable inputs, outputs, exceptions, side effects, lifecycle/ownership, persistence, invariants, and callers. Keep the source docstring close to the code; use the knowledgebase for navigable cross-module understanding and deeper reference.
- Do not put secrets, tokens, personal data, or live account credentials in docs, fixtures, examples, logs, or knowledgebase entries. Use synthetic identifiers and redacted examples.
- If the project later restores generated knowledgebase areas, follow their generator's ownership: never hand-edit generated/read-only output. Regenerate only through the documented generator after source changes and review the generated diff. Do not claim the knowledgebase currently has tooling or protections unless verified.

`docs/` exists to describe the projects structure in a user-friendly way and how to use its available commands and the entire LibzentSuite.

- Future plans and direction of design also lives in docs.
- Treat existing docs as a copy of what is available in the codebase and its functionalities rather then considering it as the source of truth.
- The docs folder exists just to keep the functionalities, commmands and their uses in a user readable format entirely separate to the knowledgebase folder.
- If there is any new addition or removal of feature or command and functionality note down it in the related files inside docs folder if available and always keep the docs updated of any changes in commands or functionalities to avoid keeping stale dumps.
- Docs mostly covers the `Plugin` and `API` related endpoints or settings available to them, to streamline the process for users that want to create plugins they should read it and get to know the available functions they could use to make them.
- The docs must provide the full knowledge of running and working with this app both from dashboard and cli interface, with clear detailed instructions from installations to the running suite and important envs or data that user need to fill in before running it.
- Note the runtime enviornment and lack of tests in other devices explicitly as the project was entirely made in linux hence wasn't tested in other platforms (this line subjected to be changed when required tests and full suite runs green in other devices aswell and will be updated in docs).

## 6. Public contracts, architecture, and UI

- Read `docs/architecture/architecture.md` for ownership and architectural boundaries and `docs/architecture/integration.md` for the current inter-module contract. Verify those documents against current code when they appear stale; do not perpetuate a known contradiction. Propose corrections to the contract docs when necessary.
- Keep `core.api.ControlAPI` the shared application facade for CLI and dashboard. Add or modify methods there when a feature legitimately requires a shared operation, and update implementations, both interfaces, and tests consistently.
- The dashboard may change its layout, navigation, component structure, and interaction flow without changing backend behavior. An old page list or prior implementation is not automatically a permanent UI requirement unless reaffirmed in the current task or a current binding contract.
- For UI work, specify a component's purpose, inputs/outputs, interaction states, disabled/loading states, success/failure feedback, validation, focus/keyboard behavior, responsive behavior, and accessible name/state. Judge acceptance by observable behavior and user outcomes, not by a required markup trick or presence of a comment.
- Preserve theme-system customization. Prefer design tokens and existing theme-aware variables over hard-coded colors, dimensions, and font choices. Components should remain usable across supported themes and density settings.
- Keep framework-owned views and plugin-authored/builder views within their established boundaries. Plugins should not gain arbitrary access to the DOM, backend internals, or global application state.
- Never solve a frontend issue by bypassing backend validation, authorization, transaction semantics, state transitions, or persistence guarantees.

## 7. Testing and regression prevention

- Add or update tests for every meaningful behavior change. Test the externally observable contract and the regression that motivated the change, not merely the implementation text or internal helper name.
- Prefer deterministic tests: injected clocks, seeded randomness, in-memory repositories/databases, simulator adapters, explicit synchronization, and bounded waits. Avoid fragile wall-clock sleeps and real network access in ordinary tests.
- Cover happy paths, invalid input, missing/unknown data, boundary values, failure recovery, cancellation, duplicate calls, and relevant concurrency/race conditions. For stateful operations, assert both the result and persisted state.
- For API or model changes, test serialization and compatibility at the boundary. For UI changes, test interaction states, stale-data/loading/error flows, accessibility-relevant keyboard behavior, responsive layout where tooling supports it, and backend persistence where relevant.
- Run the narrowest relevant tests first. Then run the adjacent unit/integration tests. Run the full suite before claiming repository-wide correctness when practical; if it cannot be run, state exactly what was and was not run.
- Do not weaken tests, delete assertions, skip failures, or loosen validation just to achieve a green run. Fix the cause or document a genuinely pre-existing failure with reproducible evidence.
- Do not run tests without cleaning up the residual cached data that was produced and kept on the previous run, start tests with fresh point.

## 8. Formatting, linting, and type checking

Use **Ruff for Python linting and formatting** and **mypy for static type checking**. Use stable releases compatible with Python 3.10+, check the tools' current official documentation when adopting new options, and keep one canonical configuration (prefer a root `pyproject.toml`). Do not maintain competing Ruff/Black/isort configurations for the same job.

Ruff and mypy were adopted 2026-10-25: the canonical configuration lives in
the root `pyproject.toml`, dependencies are declared in `[project]` with dev
tools in the `[dependency-groups] dev` table, and `uv.lock` is the pinned
universal resolution — CI installs it with `uv sync --frozen` and runs every
check through `uv run --frozen` (ruff's `F` rules superseded the former
pyflakes step). `requirements.txt` is a generated pip-compat export of the
lock (`uv export --format requirements-txt --no-hashes --no-dev
--no-emit-project -o requirements.txt`) — regenerate it, never hand-edit.
The export-sync and lock-integrity gates in CI enforce this mechanically.
The mypy strict run is a ratchet gate (`.github/mypy-baseline`): CI fails
when the error count grows past the baseline, and the baseline is only
lowered after errors are fixed. The baseline reached ZERO on 2026-11-15
(485 legacy errors fixed across all 108 source files) — strict mode is
now a hard floor: any new strict error fails CI, and the baseline must
stay at zero. Never add a blanket ignore or a blanket suppression to
shrink the count.

### Ruff baseline

Recommended initial configuration (adjust only with a documented reason):

```toml
[tool.ruff]
target-version = "py310"
line-length = 100

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "W", "I", "UP", "B", "SIM", "C4", "PIE", "RUF", "TCH"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
line-ending = "auto"
docstring-code-format = true
```

- Run `ruff check` and `ruff format --check` in CI; run `ruff format` locally for files being changed.
- Ruff owns import sorting (`I`) and formatting. Do not add a separate formatter or import sorter unless there is a demonstrated incompatibility.
- Avoid enabling unstable/preview rules by default. Add additional lint families intentionally and fix or narrowly justify exceptions.
- Prefer safe auto-fixes, but inspect every resulting diff. Never auto-fix blindly across unrelated files.
- Ignore an individual rule only with a concrete reason and the narrowest scope possible. Do not suppress whole categories or entire modules merely to silence debt.

### mypy baseline

Recommended starting point for production code in `pyproject.toml`:

```toml
[tool.mypy]
python_version = "3.10"
strict = true
warn_unreachable = true
warn_unused_configs = true
show_error_codes = true
pretty = true
files = ["core", "adapters", "database", "interfaces", "plugins", "benchmark", "scripts"]
```

- Use strict checking for new modules and materially changed code. If the legacy tree prevents an immediate clean whole-tree run, track the existing errors and expand coverage gradually; do not relax typing for new code to match the weakest legacy module.
- Resolve type errors by improving the model or boundary validation first. Avoid `Any`, `cast`, `type: ignore`, `# noqa`, and unchecked deserialization as shortcuts.
- Every suppression must use a specific error code where supported, explain why the checker cannot express the invariant, and be as local as possible. Remove obsolete suppressions when the code changes.
- Do not use `ignore_missing_imports = true` globally. Apply a narrow per-module exception only when a dependency genuinely lacks usable typing and a safer stub/adapter is not justified.
- Treat mypy's strict-mode contents as version-sensitive: use a current stable mypy release and review its documented strict flags when upgrading. Keep `warn_unreachable` and `warn_unused_configs` enabled to catch dead branches and stale configuration.
- Use `reveal_type()` temporarily for investigation, but do not leave debugging artifacts in committed code.

### Suggested local commands

```bash
ruff check .
ruff format --check .
mypy
python -m unittest discover -s tests -t . -v
```

Local checks: `ruff check`, `ruff format --check`, `mypy` (advisory until the
baseline clears), then the suite. Do not imply a command succeeded unless it
was actually run.

Official references:
- Ruff configuration and formatter: https://docs.astral.sh/ruff/configuration/ and https://docs.astral.sh/ruff/formatter/
- mypy configuration: https://mypy.readthedocs.io/en/stable/config_file.html

## 9. Dependency and compatibility discipline

- Preserve the project's declared Python minimum and runtime dependency policy. New packages must be justified; development-only tools belong in development/CI configuration, not runtime dependencies.
- Avoid relying on APIs newer than Python 3.10 unless the project intentionally raises its supported minimum and updates CI and documentation accordingly.
- Prefer explicit protocol/adapter boundaries over version-specific behavior spread throughout the codebase. Centralize compatibility logic when it is actually needed.
- For third-party API behavior that may have changed, verify current official documentation and the installed version rather than relying on memory.

## 10. Change completion checklist

Before declaring a change complete:

- [ ] Read the relevant implementation and existing tests.
- [ ] Confirmed the change respects module ownership and existing contracts, or updated the root contract intentionally.
- [ ] Removed duplicated logic or reused the canonical owner where appropriate.
- [ ] Added clear types, useful docstrings, and a model appropriate to the data.
- [ ] Added regression tests for the behavior and important failure paths.
- [ ] Updated knowledgebase reference material using its append-only policy, without adding rules there.
- [ ] Ran formatting, linting, typing, and tests that are actually available and relevant; recorded any checks not run.
- [ ] Reviewed the complete diff for unrelated changes, stale comments, accidental API breakage, credentials, and dead code.

## 11. Naming Conventions

Choose names that are **creative, expressive, memorable, and informative**. Class, method, function, and variable names should communicate purpose clearly while sounding natural and fitting the project's domain. Prefer thoughtful, distinctive names over generic placeholders, cryptic abbreviations, or excessively verbose descriptions.

- **Classes and dataclasses:** Use meaningful nouns or domain concepts that communicate identity and responsibility.
- **Methods and functions:** Prefer clear action-oriented names that describe what they accomplish.
- **Variables and attributes:** Choose descriptive names that convey intent, context, and expected values.
- **Booleans:** Use readable names such as `is_ready`, `has_access`, or `can_retry`.
- **Consistency:** Follow Python naming conventions (`PascalCase` for classes, `snake_case` for functions, methods, and variables) and maintain consistent terminology across modules.
- **Creative, not confusing:** Memorable names are encouraged when they improve understanding, but avoid forced cleverness, unexplained metaphors, misleading names, and unnecessary abbreviations.

Every name should help another developer understand the code without having to reverse-engineer its intent.

## 12. Commit Discipline

Onwards commit discipline will be reside in agents.md and there won't be a separate file for it.

Before committing:

- Work happens on a **feature branch** — verify it is active before the
  first edit (`git branch --show-current`). Never commit on `dev`/`main`.
- **Stop running services** that host the changed code (dashboard,
  bots) so the committed state is the tested state.
- Run the relevant test suites and record the outcome. **A red suite
  blocks the commit series** — fix first, then commit.
- Review the full diff (`git diff` + `git status --porcelain`). Never
  commit a file you cannot describe.

Commit granularity — one logical unit per commit:

- **One file = one commit** when the file carries its own concern
  (13 files → 13 commits, each describing that
  file's changes).
- A cohesive new feature may group its own package files into ONE
  PR, with its tests and docs as separate follow-up in pr-commits.
- Commit in **dependency order**: foundation → feature → tests → docs.

Commit messages:

- Format: `type(scope): what changed — and the user-visible why`
  (`feat parsers:`, `fix dashboard:`, `test`, `docs`, `chore` — repo
  convention).
- The message describes **the changes in the committed files
  specifically** — not a vague round summary.
- Every behavioral fix names its trigger (user report, audit finding,
  ticket) in the body when useful.

Scope & safety:

- **Never** `git push`, `reset`, `clean`, or switch branches unless
  explicitly authorized in the same breath as the work.
- **Never** discard unrelated user changes. Unrelated modified files
  stay uncommitted (or are committed separately with an accurate
  message if the user asked for "all changes").
- Commit only after the final diff review. The working tree ends
  **clean** — everything relevant committed, nothing extra.
- Push to remote only when there are five or more commits have been committed.
- **Never** push committed series to the remote without owner's explicit approval.

After the series:

- Show `git log --oneline` for the new commits + final `git status`.
- State what was intentionally left uncommitted and why.

## 13. Skills & Tools.

notable mentions of skills that worth using for more efficiency and speed.
other skills related to the project resides in `.agents/`.

[FAST-EDIT]

When you need to edit, create, write, or save files:

1. Load the fast-edit skill first: skill("fast-edit")
2. Use fast-edit commands (show/replace/insert/delete/batch/paste/write/generate) instead of the built-in Edit/Write tools.
3. For batch edits or multi-file writes, prefer fast-batch and fast-write.
4. For user-pasted content, prefer save-pasted (zero token, zero escaping).

