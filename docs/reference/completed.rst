Completed Features
=================

The user-facing historical record — what shipped, when, and where its
detail lives. The full engineering detail is in the local
``knowledgebase/`` archive (gitignored; on the machines that built
it).

Workspace Unification (2026-11-13 → 2026-11-14)
-----------------------------------------------

ONE concept (the **workspace**) replaced sync groups + the fleet
module. Workspaces own membership (the ``accounts.workspace``
pointer), pacing clocks (shared serialization), and chains
("account A acts → follow-up account B responds, visibly"). The old
modules and their five tables were deleted; migration v10 folded the
data, v11 dropped the old shapes.

**Key files:** ``core/workspace/`` (service, models, transitions,
policy, chains runtime, durable signals, guard, reconciler)

Command Convergence (2026-11-14 → 2026-11-15)
---------------------------------------------

Plugins declare their command face as a real library **Cog**
(``COG = MyCog`` on PluginBase) — typed converter parameters,
command groups, native help, cog hooks. The manual COMMANDS facade
(CommandSpec/ArgSpec/tokenization/registration bridge) retired. All
15 framework verbs ride the FrameworkVerbsCog. The hub's command
machinery, the RemoteControl mention bridge, and the dashboard-era
facade residue retired to the ``dump/`` wing (2026-11-15) — preserved
verbatim with a full inventory.

**Key files:** ``core/plugin_manager/cog_host.py``,
``core/plugin_manager/framework_cog.py``

better.py-self Fork (fully adopted)
-----------------------------------

The user-account Discord client fork: memory-capped caches,
per-account cache persistence, dynamic callable prefixes, the
authorization gate via ``bot.add_check()`` (evaluated before argument
parsing), the durable signal outbox, and ``cache_stats()``.

**Key files:** ``adapters/discord/client.py``,
``core/workspace/signals.py``

The Two-Level Data Model (2026-11-15 → 2026-11-16)
---------------------------------------------------

The owner-locked architecture: **account plugin profiles** handle
independent execution (plugin-defined schema, channels-in-profile,
profile-scoped cooldowns and task attribution); **workspace presets**
coordinate multiple accounts as a group (reference profiles, never
copy; own workflow state and execution dependencies). All eight
phases landed, migrations v12–v16:

- The store: ``(account, plugin, name)`` identity, full-scope CRUD
  predicates, opaque bodies (the core never interprets), cascade
- The lifecycle: the facade's profile verbs carry **zero scope
  parameters** — isolation by construction; the active-profile
  pointer with atomic clear-on-delete
- The runtime: ``profile_settings()`` resolution seam; profile-scoped
  cooldowns (``''`` = the profile-less scope every existing caller
  keeps) and task attribution with scoped cancel
- The interfaces: REST routes + the CLI ``profiles`` group
- The legacy fold (v15): template bindings became account-owned
  instances, idempotent by construction
- Reference-based presets + executions (v16): apply activates in
  place; drift detected and fails closed; checkpoints resume without
  replaying

**Key files:** ``database/repositories/account_plugin_profiles.py``,
``database/repositories/workspace_presets.py``,
``core/workspace/service.py`` (the P8 surface)

Quality Campaigns
-----------------

**mypy strict ZERO — both trees.** Production: 485 legacy errors
fixed (2026-11-15), surfacing four latent runtime bugs along the way.
Tests: 2,443 errors fixed by a six-batch annotation campaign
(2026-11-16) — zero assertions weakened, zero tests deleted. Strict
mode over all 216 source files is a hard floor; the CI ratchet
requires the error count to equal the baseline exactly (grow fails,
shrink without lowering the baseline also fails).

**The toolchain settles (2026-11-21).** pytest + xdist owns the test
runner (``-n auto --dist loadscope`` — each gate leg drops from
~7.5 minutes serial to ~2m45s; ``unittest discover`` retired).
pyright takes static typing from mypy, which retired with both its
trees at ZERO — re-adoption, if ever needed, is ``uv add --dev mypy``
a clean tree. The pyright ratchet opened at 72 and marched to 0 in
one campaign; the equality rule holds on both sides of zero.
``scripts/local-ci.sh`` is now the clean seven-step code-quality
gate — the reviewer machinery left it entirely (CodeRabbit reviews
PRs on GitHub; it was never a CI check).

**Coverage floor 76%** (production-only, measured 77.7%) — branch
coverage over the eight production trees, per-leg in CI.

**The 3.10–3.13 matrix** on the exact uv lock — the widened floor
caught the parallel-first-boot WAL race and the 3.10-only
``Task.cancelling`` assumption. ``scripts/local-ci.sh`` mirrors the
entire CI gate locally (including the isolated 3.10 leg) — nothing
reaches CI that the local gate did not see first.

FastAPI Core API (2026-11-14)
-----------------------------

``interfaces/api/`` — the REST surface over the one ControlAPI
facade: Pydantic models, X-API-Token auth, the full account/workspace/
plugin/task/log/audit surface including the profile routes. Honest
dependency extras: ``[api]`` (uvicorn), ``[ui]`` (flet + httpx),
``[all]``.

**Key files:** ``interfaces/api/app.py``, ``pyproject.toml``

The Direct Client Connection (2026-11-17)
-----------------------------------------

Owner-locked architecture change, same day: the framework's value is
ORCHESTRATION and multi-account isolation, not capability mediation.
Plugin cogs receive their account's library client DIRECTLY through
the toolbox (``CogServices.bind_client`` at attach — account-scoped
by construction, dies with the cog). AGENTS §1.4's "plugins never
touch clients" retired; the paced pipeline remains the law for
coordinated outbound action, and the facade keeps what has no
client-side equivalent (persistence families, orchestration verbs).
The unified-object convention: ``self.services.client`` (alias
``self.bot``) + ``self.services.paced`` — the facade rides the
toolbox, not the client's constructor (order + cardinality, recorded
in the plan).

**Key files:** ``core/plugin_manager/cog_host.py``,
``docs/plans/client-capability-adoption.md`` (the remaining tracks)

The Plugin COG Seam — ctx.services (2026-11-17)
-----------------------------------------------

Roadmap #4 (the host paced-facade gap): plugin cogs reach the paced
pipeline from their commands. The cog host's toolbox
(:class:`core.plugin_manager.cog_host.CogServices`) binds the
per-(account, plugin) ``PluginServices`` facade at attach time
(fail-closed — unbound is absent, never another account's pipeline);
the account manager's attach loop hands each cog its own facade from
``services_for``; the client's ``before_invoke`` lifts the bound
facade onto ``ctx.services`` (framework verbs carry no toolbox, so
they stay untouched). Ping's command face landed as the first real
plugin COG — ``<prefix>ping <channel_id> <text...>`` rides the paced
pipeline and replies via ``ctx.send`` (the one documented
library-native use); its ``_ping`` internals retired into the cog.

**Key files:** ``core/plugin_manager/cog_host.py``,
``adapters/discord/client.py``, ``plugins/ping/plugin.py``

Flet UI Shell (2026-11-14)
--------------------------

The dashboard-replacement shell: page registry, API client, flet
NavigationRail with Status/Accounts/Workspaces/Logs pages. Flet
imports lazily (testable without the UI dependency). The full
interface per the owner's design contract is the active queue's #1
item.

**Key files:** ``interfaces/ui/__init__.py``,
``docs/plans/flet_ui_design_contract.md`` (the spec)

Repository Health Stack (2026-11-15 → 2026-11-16)
-------------------------------------------------

Security workflow (gitleaks full-history, pip-audit against the exact
lock, bandit), weekly health sweep (fresh-runner full gate, build
sanity, action-pin validation, dependency-freshness probe), per-push
build gate, the unarmed-but-ready release pipeline (CHANGELOG-driven
notes), Dependabot (github-actions), native secret scanning + push
protection, CodeQL, CODEOWNERS, SECURITY.md, branch/tag rulesets, and
the CodeRabbit reviewer (V2-tuned: risk instructions, per-path
contract instructions, regex base branches, ruff config).
