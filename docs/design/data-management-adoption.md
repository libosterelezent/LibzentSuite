# Plugin Profile + Workspace Preset Adoption Plan

## The two-level model

**Account-level plugin profiles** handle independent plugin execution.
**Workspace presets** coordinate multiple accounts and their execution
as a group. Both concepts exist with distinct responsibilities — the
workspace preset is NOT another universal plugin-configuration system.

## Proposed hierarchy

```
WORKSPACE (coordination layer)
├── Workspace Preset: "Mudae Farming"
│   ├── Account A: roll/main, claim/default
│   ├── Account B: roll/secondary, claim/default
│   └── Account C: roll/main, claim/priority
│   ├── Execution order and coordination rules
│   └── Shared workflow state and progress
│
│   Each account retains ownership of its plugin profiles.
│   The workspace preset REFERENCES and coordinates them.
│
ACCOUNT A (independent execution)
├── Roll plugin → "main" profile (channels, cooldowns, config)
└── Claim plugin → "default" profile

ACCOUNT B (independent execution)
├── Roll plugin → "secondary" profile
└── Claim plugin → "default" profile

ACCOUNT C (independent execution)
├── Roll plugin → "main" profile
└── Claim plugin → "priority" profile
```

## Responsibility boundaries

| Capability | Account plugin profile | Workspace preset |
|---|---|---|
| Plugin configuration | **Owns** | References |
| Channel configuration | **Owns** | References indirectly |
| Cooldown state | **Owns** (profile-scoped) | Tracks coordination dependencies |
| Individual execution task | **Owns** | Orchestrates |
| Account-specific runtime state | **Owns** | Does not replace |
| Multi-account workflow state | — | **Owns** |
| Cross-account synchronization | — | **Owns** |
| Execution dependencies | — | **Owns** |

## Streamlined implementation roadmap

### Phase 1 — Establish the ownership model

1. Profile identity: `(account_id, plugin_id, profile_name)` — unique
   within each account+plugin.
2. Plugin-defined profile schemas — the core never interprets
   plugin-specific fields.
3. Channel configuration lives inside the profile (plugin-owned).
4. Profile-scoped cooldowns: `(account, plugin, profile, cooldown_key)`.
5. Profile-scoped tasks: each profile can have its own execution
   context, identified by profile name.
6. Independent scheduling: a cooldown or task in one profile never
   blocks another profile.
7. Profile-specific channels: each profile executes on its own
   configured channel(s).
8. Independent lifecycle: starting/stopping/editing/removing one
   profile must not affect another.
9. Restart recovery: cooldowns and eligible tasks recover
   independently per profile.

| Level | Responsibility |
|---|---|
| Account | Owns its plugin instances and data |
| Plugin | Owns its profile collection and profile behavior |
| Profile | Owns its configuration, cooldown, and execution lifecycle |

### Phase 2 — Implement core persistence

1. Generic plugin-profile storage API: create/read/update/delete/
   list/exists.
2. Store profile data as structured JSON — the core never interprets
   plugin-specific fields.
3. Enforce account + plugin ownership on every operation.
4. SQLite constraint: `UNIQUE(account, plugin, profile_name)`.
5. Profile schema versioning: the plugin can evolve its profile shape.

### Phase 3 — Integrate the plugin lifecycle

1. Plugins define their own profile defaults and validation.
2. Profile creation/editing/deletion/listing/selection is
   plugin-managed (the cog's own commands or lifecycle hooks).
3. Profiles load using the active (account, plugin) context.
4. Plugins manage their own active profile and execution behavior.
5. Channel selection and plugin settings live inside the profile.

### Phase 4 — Connect runtime execution

1. Resolve the selected profile before starting a plugin task.
2. Pass the resolved profile configuration into the execution layer.
3. Each account's plugin instance uses only its own profiles.
4. Profile changes in one account never affect another's running tasks.
5. Runtime state (cooldowns, task progress) is handled separately
   from persisted configuration.

### Phase 5 — Integrate the application interfaces

1. Expose generic profile persistence through the Core API.
2. Each plugin provides its own profile-management operations (cog
   commands, lifecycle hooks).
3. CLI and GUI consume those operations without imposing a universal
   profile schema.
4. Display profile information using plugin-provided metadata.

### Phase 6 — Test isolation and lifecycle

1. Identical profile names across different accounts — no collision.
2. Identical profile names across different plugins — no collision.
3. Profile CRUD + persistence across restarts.
4. Account switching + concurrent plugin execution.
5. Channel settings isolated within their respective profiles.
6. Deletion, active-profile changes, and interrupted runtime recovery.

### Phase 7 — Migrate existing plugin settings

1. Map existing `plugin_profiles` + `account_plugins.profile` bindings
   into plugin-owned profile instances.
2. Extract the reserved `"channels"` key from profile settings into
   the profile's channel configuration.
3. Preserve account-specific settings and channel assignments.
4. Migrate without losing profile identity or configuration.
5. Remove redundant centralized preset mechanisms after verification.

### Phase 8 — Workspace preset integration

1. A workspace preset REFERENCES `(account, plugin, profile_name)`
   tuples — it never copies profile configuration.
2. Workspace execution instances hold runtime state: workflow
   progress, dependency checkpoints, coordination signals,
   participating account status.
3. A workspace preset must only reference accounts that are members
   of that workspace.
4. If a referenced profile is renamed/removed/modified, the workspace
   detects the change rather than silently executing against a
   different profile.
5. Persist workflow checkpoints so execution can resume after
   interruptions without duplicating completed operations.
6. Workspace coordination may OBSERVE account/profile state but never
   becomes the owner of individual plugin cooldowns or configuration.

### Identity structure

```
# Account-owned profile
(account_id, plugin_id, profile_name)

# Workspace-owned preset (orchestration definition)
(workspace_id, preset_name)

# Workspace execution instance (running/recoverable)
(workspace_id, preset_id, execution_id)

# Workspace participant reference
(execution_id, account_id, plugin_id, profile_name)
```

These are conceptual identities; the actual schema uses internal IDs
and foreign keys.

## Important implementation decisions

1. **Reference profiles, don't copy them.** A workspace preset
   references the (account, plugin, profile) it intends to use — no
   duplicated configuration.
2. **Keep runtime state separate.** Individual cooldowns remain
   profile-scoped. Workspace state tracks workflow progress,
   dependencies, coordination signals, and account status.
3. **Define workspace membership explicitly.** A preset only
   references member accounts.
4. **Handle profile changes safely.** Renamed/removed profiles are
   detected, not silently substituted.
5. **Make coordination recoverable.** Workflow checkpoints enable
   resume-after-interrupt without duplication.
6. **Avoid shared-state ownership ambiguity.** The workspace observes
   but never owns individual plugin state.

## Final architecture

- **Profile:** Defines how one plugin operates for one account.
- **Workspace preset:** Defines how multiple account profiles work
  together — a reusable orchestration definition.
- **Workspace execution:** Holds the runtime state of one coordinated
  workflow — a particular running/recoverable instance.
- **Core:** Provides persistence, ownership enforcement, lifecycle
  management, and execution infrastructure.

## Migration contract (existing → new)

| Existing | Maps to | Notes |
|---|---|---|
| `plugin_profiles.settings_json` | The profile's initial JSON body | Plugin validates against its own schema |
| `plugin_profiles` row | One profile instance under `(account, plugin, name)` | The presets table becomes workspace-preset definitions |
| `account_plugins.profile` (binding column) | The active profile reference | Becomes a `(account, plugin, profile_name)` lookup |
| `account_plugin_channels` bindings | The profile's internal channel list | Extracted from the reserved `"channels"` key |
| `plugin_settings` overrides | Merged into the profile's JSON | The plugin owns resolution after migration |
| Preset provenance (`source_preset_id` etc.) | Dropped — profiles are not template-copies | The workspace preset references, not copies |
| `workspace_chains.definition_json` | The workspace preset's orchestration rules | Steps become participant references |

## Final architectural boundary

| Responsibility | Owner |
|---|---|
| Profile schema | Plugin |
| Profile defaults and validation | Plugin |
| Profile CRUD and selection behavior | Plugin |
| Channel configuration | Plugin profile |
| Profile persistence | Core |
| Account isolation | Core |
| Plugin isolation | Core |
| Runtime execution | Plugin, managed by Core |
| Workspace orchestration rules | Workspace preset |
| Multi-account workflow state | Workspace execution |
| Cross-account synchronization | Workspace execution |

**Implementation principle:** The Core provides storage and isolation
infrastructure — it does not dictate how plugins organize their
profiles. Each plugin retains control over its own profile system.
Workspace presets coordinate; they never become the configuration
owner.
