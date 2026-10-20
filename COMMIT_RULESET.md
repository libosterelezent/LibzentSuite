# COMMIT RULESET — how every change lands in this repository

> Established 2026-10-21 (Mudae parser / dashboard-hardening round).
> Every future commit series follows this exactly.

## 1. Before committing

* Work happens on a **feature branch** — verify it is active before the
  first edit (`git branch --show-current`). Never commit on `dev`/`main`.
* **Stop running services** that host the changed code (dashboard,
  bots) so the committed state is the tested state.
* Run the relevant test suites and record the outcome. **A red suite
  blocks the commit series** — fix first, then commit.
* Review the full diff (`git diff` + `git status --porcelain`). Never
  commit a file you cannot describe.

## 2. Commit granularity — one logical unit per commit

* **One file = one commit** when the file carries its own concern
  (the dashboard round: 13 files → 13 commits, each describing that
  file's changes).
* A cohesive new feature may group its own package files into ONE
  commit (the parser engine: `parsers/mudae/**` landed together),
  with its tests and docs as separate follow-up commits.
* Commit in **dependency order**: foundation → feature → tests → docs.

## 3. Commit messages

* Format: `type(scope): what changed — and the user-visible why`
  (`feat parsers:`, `fix dashboard:`, `test`, `docs`, `chore` — repo
  convention).
* The message describes **the changes in the committed files
  specifically** — not a vague round summary.
* Every behavioral fix names its trigger (user report, audit finding,
  ticket) in the body when useful.

## 4. Scope & safety

* **Never** `git push`, `reset`, `clean`, or switch branches unless
  explicitly authorized in the same breath as the work.
* **Never** discard unrelated user changes. Unrelated modified files
  stay uncommitted (or are committed separately with an accurate
  message if the user asked for "all changes").
* Commit only after the final diff review. The working tree ends
  **clean** — everything relevant committed, nothing extra.

## 5. After the series

* Show `git log --oneline` for the new commits + final `git status`.
* State what was intentionally left uncommitted and why.
