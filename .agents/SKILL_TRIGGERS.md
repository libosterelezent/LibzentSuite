# Skill triggers — the routing table

Load the matching skill BEFORE starting the work, not after. When a
user prompt or task matches a trigger below, invoke the skill tool with
that id first; its instructions then govern how the work proceeds. Two
skills matching? Load both — they compose (e.g. fast-edit +
systematic-debugging on a failing edit).

The binding engineering standards live in the root `AGENTS.md`; this
table only routes work to skills. Skills themselves never override
AGENTS.md.

## Always-on (this repository)

| Skill | Load when | Example user prompts |
|---|---|---|
| `fast-edit` | ANY file edit, create, write, or save — before the first edit of the session | (every editing task) |
| `systematic-debugging` | any bug, test failure, hang, or unexpected behavior — BEFORE editing to fix | "tests fail", "it hangs", "why did this break" |
| `test-driven-development` | fixing a defect or changing behavior a test can express | "fix the cooldown bug", "make X do Y" |
| `ci-failure-investigation` | any red X, timeout, or stuck GitHub Actions run | "CI is red", "check the failing run" |
| `verification-before-completion` | before claiming tests green / CI fixed / work complete | (before every completion claim) |

## Domain triggers

| Skill | Load when | Example user prompts |
|---|---|---|
| `multi-account-isolation` | touching account-scoped state: ownership, plugins, guild/channel data, workers, events, caches, persistence, credentials, concurrency, recovery | "rework account plugin binding", "who owns this cache?" |
| `security-review` | scanning for vulnerabilities; injection, secrets, access control questions | "is my code secure?", "audit this endpoint" |
| `fastapi` | work on `interfaces/api/` — routers, Pydantic models, SSE, serving the frontend | "add a /api/workspaces route" |
| `test-flet-apps-dev` | testing/debugging the Flet app in dev mode (web/desktop/browser verification) | "run the UI and check the logs page" |
| `webapp-testing` | interacting with local web apps via Playwright (screenshots, console/network logs) | "open the dashboard and click through" |
| `library-skills` | installing, refreshing, or managing agent skills from packages | "install that skill pack" |

## Catalog skills (harness-provided; load on matching work)

| Skill | Load when |
|---|---|
| `api-design` / `code-quality` / `library-review` | designing or reviewing Python APIs, lint/typing posture, or a full library health review |
| `packaging` / `release-management` | pyproject/build/publish work; versioning, changelogs, release automation |
| `git-hygiene` | suspected committed secrets/artifacts; .gitignore audits; history scrub questions |
| `documentation` | docstring/sphinx/README overhaul passes |
| `testing-strategy` / `performance` | designing test suites; profiling and optimization campaigns |
| `design-taste` family (`taste-*`, genre packs) | UI design, restyling, or component work on the Flet interface — load `design-taste` first, then the genre pack that matches the brief |
| `taste-design-review` | critiquing an existing UI |
| `reverse-engineer-anything` / REA skills | binary/artifact reverse-engineering tasks (not ordinary source analysis) |

## Anti-triggers (do NOT load for these)

- Ordinary source-repository architecture questions → read
  `docs/architecture/architecture.md` instead; no skill needed.
- The knowledgebase folder → read-only historical knowledge; never a
  skill target.
- Python questions answered by the stdlib docs → no skill.
