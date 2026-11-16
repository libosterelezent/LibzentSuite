"""Ordered SQL migrations. Append-only: NEVER edit an applied migration.

Each entry: (version, name, sql). The runner (database/db.py) records applied
versions in `schema_migrations` and applies pending ones in order inside a
transaction per migration.
"""

from __future__ import annotations

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial schema",
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            token_ref   TEXT,
            enabled     INTEGER NOT NULL DEFAULT 1,
            sync_group  TEXT,
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at  REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS account_settings (
            account    TEXT NOT NULL,
            key        TEXT NOT NULL,
            value_json TEXT NOT NULL,
            PRIMARY KEY (account, key)
        );
        CREATE TABLE IF NOT EXISTS plugins (
            name            TEXT PRIMARY KEY,
            version         TEXT NOT NULL DEFAULT '0.0.0',
            description     TEXT NOT NULL DEFAULT '',
            settings_json   TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS plugin_settings (
            account    TEXT NOT NULL,
            plugin     TEXT NOT NULL,
            key        TEXT NOT NULL,
            value_json TEXT NOT NULL,
            PRIMARY KEY (account, plugin, key)
        );
        CREATE TABLE IF NOT EXISTS plugin_states (
            account TEXT NOT NULL,
            plugin  TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (account, plugin)
        );
        CREATE TABLE IF NOT EXISTS profiles (
            name        TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS profile_plugins (
            profile TEXT NOT NULL,
            plugin  TEXT NOT NULL,
            PRIMARY KEY (profile, plugin)
        );
        CREATE TABLE IF NOT EXISTS account_profiles (
            account TEXT NOT NULL,
            profile TEXT NOT NULL,
            PRIMARY KEY (account, profile)
        );
        CREATE TABLE IF NOT EXISTS rate_limit_policies (
            level      TEXT NOT NULL,
            owner      TEXT NOT NULL DEFAULT '',
            action     TEXT NOT NULL,
            interval   REAL NOT NULL,
            jitter_min REAL NOT NULL DEFAULT 0.0,
            jitter_max REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (level, owner, action)
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL NOT NULL,
            account    TEXT NOT NULL,
            direction  TEXT NOT NULL,
            action     TEXT NOT NULL,
            channel_id INTEGER,
            content    TEXT,
            wait_ms    REAL NOT NULL DEFAULT 0,
            jitter_ms  REAL NOT NULL DEFAULT 0,
            note       TEXT
        );
        CREATE INDEX IF NOT EXISTS audit_ts ON audit_logs (ts DESC);
        CREATE TABLE IF NOT EXISTS logs (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       REAL NOT NULL,
            severity TEXT NOT NULL,
            source   TEXT NOT NULL,
            account  TEXT,
            plugin   TEXT,
            task_id  INTEGER,
            event    TEXT,
            message  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS logs_ts ON logs (ts DESC);
        CREATE TABLE IF NOT EXISTS task_history (
            id           INTEGER PRIMARY KEY,
            name         TEXT,
            account      TEXT NOT NULL,
            plugin       TEXT,
            action       TEXT NOT NULL,
            status       TEXT NOT NULL,
            priority     INTEGER NOT NULL DEFAULT 1,
            submitted_at REAL NOT NULL,
            started_at   REAL,
            finished_at  REAL,
            attempts     INTEGER NOT NULL DEFAULT 0,
            duration_s   REAL,
            error        TEXT,
            summary      TEXT
        );
        CREATE INDEX IF NOT EXISTS task_history_account ON task_history (account, id DESC);
        CREATE TABLE IF NOT EXISTS kv (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        "persistent api cache",
        """
        -- Per-account cache of Discord entities resolved through the REST
        -- API (DM channels, users) so restarts never re-fetch them: one
        -- open_dm per (account, user) EVER, not per boot (anti-flag: the
        -- framework outside plugins makes the minimum possible API calls;
        -- rows are scoped to ONE account/profile only).
        CREATE TABLE IF NOT EXISTS api_cache (
            account    TEXT NOT NULL,
            kind       TEXT NOT NULL,   -- 'dm' | 'user' | 'channel'
            key        TEXT NOT NULL,   -- str(entity id)
            value_json TEXT NOT NULL,
            cached_at  REAL NOT NULL,
            PRIMARY KEY (account, kind, key)
        );
        """,
    ),
    (
        3,
        "secure credentials + persistent cooldowns",
        """
        -- Encrypted account credentials (spec §12): Fernet ciphertext ONLY;
        -- the key lives OUTSIDE the database (state/credentials.key 0600 or
        -- LZ_MASTER_KEY env) — a stolen DB never yields usable tokens.
        CREATE TABLE IF NOT EXISTS credentials (
            account    TEXT PRIMARY KEY,
            ciphertext BLOB NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        -- Generic persistent cooldowns (spec §21/§22): ABSOLUTE epoch
        -- expiry so a restart restores the REMAINING time, never restarts
        -- it. Core owns persistence/lookup/expiry; plugins own meaning.
        CREATE TABLE IF NOT EXISTS cooldowns (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            account       TEXT NOT NULL,
            plugin        TEXT NOT NULL,
            cooldown_key  TEXT NOT NULL,
            started_at    REAL NOT NULL,
            expires_at    REAL NOT NULL,
            source        TEXT NOT NULL DEFAULT '',
            reason        TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL,
            UNIQUE (account, plugin, cooldown_key)
        );
        CREATE INDEX IF NOT EXISTS cooldown_expiry ON cooldowns (expires_at);
        """,
    ),
    (
        4,
        "per-account trail indexes",
        """
        -- per-account history views walk (account, id DESC): without these a
        -- quiet account's filter scanned the whole (10k-row) trail table
        CREATE INDEX IF NOT EXISTS audit_logs_account ON audit_logs (account, id DESC);
        CREATE INDEX IF NOT EXISTS logs_account ON logs (account, id DESC);
        -- the ts indexes served no query (every trail read orders by id) and
        -- taxed the two hottest write paths
        DROP INDEX IF EXISTS audit_ts;
        DROP INDEX IF EXISTS logs_ts;
        """,
    ),
    (
        5,
        "first-class sync groups",
        """
        -- Sync groups become a first-class resource: identity + metadata
        -- live here, MEMBERSHIP stays on accounts.sync_group (the group
        -- NAME pointer) — an account belongs to at most one group, so a
        -- join table would add nothing the column does not already say.
        CREATE TABLE IF NOT EXISTS sync_groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );
        -- LEGACY CONVERSION (one group per DISTINCT name, never one per
        -- account): A->Alpha B->Alpha C->Beta yields Alpha{A,B} + Beta{C}.
        -- INSERT OR IGNORE keeps a hand-seeded table intact; names are
        -- stored VERBATIM (validation is the manager's job at create/
        -- rename time — a migration must never reject existing data).
        INSERT OR IGNORE INTO sync_groups
            (name, description, enabled, created_at, updated_at)
        SELECT DISTINCT sync_group, '', 1,
               CAST(strftime('%s', 'now') AS REAL),
               CAST(strftime('%s', 'now') AS REAL)
        FROM accounts
        WHERE sync_group IS NOT NULL AND sync_group != '';
        """,
    ),
    (
        6,
        "account plugin assignments",
        """
        -- Plugins are assigned to accounts DIRECTLY (Account -> Plugins):
        -- one row per (account, plugin) means "this account runs this
        -- plugin". The profile tables (profiles / profile_plugins /
        -- account_profiles) are left untouched for a future
        -- plugin-specific profile system but stop being the source of
        -- account capabilities. Deliberately NO data migration from
        -- account_profiles: assignments start empty and are managed by
        -- the layer above. Keyed by account NAME like account_profiles;
        -- no FK and no cascade by the same convention (cleanup on
        -- account deletion belongs to the account-delete path, as with
        -- the other name-keyed per-account tables).
        CREATE TABLE IF NOT EXISTS account_plugins (
            account TEXT NOT NULL,
            plugin  TEXT NOT NULL,
            PRIMARY KEY (account, plugin)
        );
        """,
    ),
    (
        7,
        "plugin profiles",
        """
        -- Per-plugin named setting profiles: a reusable bundle of setting
        -- values (validated against the plugin's Setting schema by the
        -- ControlAPI layer, NEVER by the repository) that an account can
        -- bind to per plugin. settings_json may carry the RESERVED
        -- top-level key "channels" (the channel-scope convention
        -- documented in core/plugin_scopes.py); every other key must be
        -- a plugin setting name.
        CREATE TABLE IF NOT EXISTS plugin_profiles (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin       TEXT NOT NULL,
            name         TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at   REAL NOT NULL,
            updated_at   REAL NOT NULL,
            UNIQUE (plugin, name)
        );
        -- The binding lives ON the assignment row: NULL (or absent) =
        -- plugin defaults, no profile in between. ALTER (not a table
        -- rebuild): the column is appended like the migration-runner
        -- convention — plain ADD COLUMN inside the per-migration
        -- transaction, run exactly once (schema_migrations guards it).
        ALTER TABLE account_plugins ADD COLUMN profile TEXT;
        """,
    ),
    (
        8,
        "drop legacy profile tables",
        """
        -- ProfileManager removal (2026-10-22): the legacy named
        -- permission-bundle tables gated nothing and their accessor
        -- layer is deleted. Capabilities live in account_plugins;
        -- per-plugin settings presets live in plugin_profiles.
        DROP TABLE IF EXISTS account_profiles;
        DROP TABLE IF EXISTS profile_plugins;
        DROP TABLE IF EXISTS profiles;
        """,
    ),
    (
        9,
        "fleet orchestration state",
        """
        -- PHASE 9 (spec §1/§2, D2 database-authoritative): the fleet's
        -- OWN projection. Membership itself stays with sync_groups +
        -- accounts.sync_group (SyncGroupManager's UNCHANGED job); these
        -- tables hold what the orchestrator adds on top: execution mode
        -- + eligibility per member, task assignments (the fleet work
        -- unit — NOT the scheduler's per-action tasks), scoped fleet
        -- cooldowns, and the modest transition history D2 requires.
        -- Keyed by group NAME (the unique pointer) and account NAME,
        -- matching the name-keyed convention of the other per-account
        -- tables; Discord snowflakes that appear in scopes are STRINGS.
        CREATE TABLE IF NOT EXISTS fleet_groups (
            group_name  TEXT PRIMARY KEY,
            paused      INTEGER NOT NULL DEFAULT 0,
            generation  INTEGER NOT NULL DEFAULT 1,
            updated_at  REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fleet_members (
            account     TEXT PRIMARY KEY,
            group_name  TEXT NOT NULL,
            mode        TEXT NOT NULL,
            eligibility TEXT NOT NULL,
            since       REAL NOT NULL,
            version     INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_fleet_members_group
            ON fleet_members(group_name);
        CREATE TABLE IF NOT EXISTS fleet_assignments (
            id            TEXT PRIMARY KEY,
            group_name    TEXT NOT NULL,
            task_id       TEXT NOT NULL,
            account       TEXT,
            status        TEXT NOT NULL,
            priority      INTEGER NOT NULL DEFAULT 0,
            created_at    REAL NOT NULL,
            started_at    REAL,
            stopped_at    REAL,
            resume_policy TEXT NOT NULL DEFAULT '',
            version       INTEGER NOT NULL DEFAULT 1,
            generation    INTEGER NOT NULL DEFAULT 1,
            error         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_fleet_assignments_group_status
            ON fleet_assignments(group_name, status);
        CREATE TABLE IF NOT EXISTS fleet_cooldowns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type  TEXT NOT NULL,
            scope_id    TEXT NOT NULL,
            reason      TEXT NOT NULL,
            started_at  REAL NOT NULL,
            expires_at  REAL NOT NULL,
            source      TEXT NOT NULL DEFAULT 'manual',
            cleared_at  REAL
        );
        CREATE INDEX IF NOT EXISTS idx_fleet_cooldowns_scope
            ON fleet_cooldowns(scope_type, scope_id);
        CREATE TABLE IF NOT EXISTS fleet_transitions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            kind            TEXT NOT NULL,
            group_name      TEXT NOT NULL,
            generation      INTEGER NOT NULL,
            reason          TEXT NOT NULL,
            correlation_id  TEXT NOT NULL,
            occurred_at     REAL NOT NULL,
            task_id         TEXT,
            assignment_id   TEXT,
            account         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_fleet_transitions_group
            ON fleet_transitions(group_name, id);
        """,
    ),
    (
        10,
        "workspace unification W2 — the v10 fold (additive)",
        """
        -- WORKSPACE UNIFICATION (WORKSPACE_DESIGN.md §13, decisions
        -- 5/9/11/15). ADDITIVE by construction: every new table is
        -- created, the data fold runs, and provenance columns are
        -- appended — while the v9 tables/columns (sync_groups, the
        -- five fleet tables, accounts.sync_group, account_plugins.
        -- profile) remain in place until W3 unwires their readers.
        -- The physical DROP is migration v11 (same series, no
        -- coexistence across releases) so every intermediate commit
        -- boots and its full suite stays green.

        -- ------------------------------------------------ workspaces
        -- Identity + orchestration state in ONE owner: generation and
        -- paused fold in from fleet_groups (decision 5/6).
        CREATE TABLE IF NOT EXISTS workspaces (
            name        TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT '',
            enabled     INTEGER NOT NULL DEFAULT 1,
            paused      INTEGER NOT NULL DEFAULT 0,
            generation  INTEGER NOT NULL DEFAULT 1,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );
        INSERT INTO workspaces (name, description, enabled, created_at, updated_at)
        SELECT name, description, enabled, created_at, updated_at
        FROM sync_groups;
        -- fleet-only groups (no sync_groups row): adopted so no
        -- orchestration state is lost; then the fold of state.
        INSERT OR IGNORE INTO workspaces (name, description, enabled, created_at, updated_at)
        SELECT fg.group_name, '', 1, fg.updated_at, fg.updated_at
        FROM fleet_groups fg;
        UPDATE workspaces
        SET generation = (SELECT generation FROM fleet_groups fg
                          WHERE fg.group_name = workspaces.name),
            paused = COALESCE((SELECT paused FROM fleet_groups fg
                               WHERE fg.group_name = workspaces.name), paused),
            updated_at = COALESCE((SELECT updated_at FROM fleet_groups fg
                                   WHERE fg.group_name = workspaces.name), updated_at)
        WHERE name IN (SELECT group_name FROM fleet_groups);

        -- ------------------------------------------- the membership
        -- pointer (decision 11): accounts.workspace is THE authoritative
        -- membership column; backfilled from the v9 pointer. The v9
        -- column stays (read-only) until v11 drops it.
        ALTER TABLE accounts ADD COLUMN workspace TEXT;
        UPDATE accounts
        SET workspace = sync_group
        WHERE sync_group IS NOT NULL AND sync_group != '';

        -- workspace_members is the PROJECTION (same-transaction
        -- updates only): folded from the pointer LEFT JOINed with
        -- fleet_members so mode/eligibility survive (group_managed ->
        -- managed is the one value rename, decision 6 vocabulary).
        CREATE TABLE IF NOT EXISTS workspace_members (
            account     TEXT NOT NULL PRIMARY KEY,
            workspace   TEXT NOT NULL,
            mode        TEXT NOT NULL DEFAULT 'independent',
            eligibility TEXT NOT NULL DEFAULT 'eligible',
            joined_at   REAL NOT NULL,
            updated_at  REAL NOT NULL,
            version     INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO workspace_members (account, workspace, mode, eligibility, joined_at, updated_at)
        SELECT a.name,
               a.workspace,
               COALESCE(CASE fm.mode WHEN 'group_managed' THEN 'managed'
                                     ELSE fm.mode END, 'independent'),
               COALESCE(fm.eligibility, 'eligible'),
               COALESCE(fm.since, a.created_at),
               COALESCE(fm.since, a.created_at)
        FROM accounts a
        LEFT JOIN fleet_members fm ON fm.account = a.name
        WHERE a.workspace IS NOT NULL AND a.workspace != '';
        CREATE INDEX IF NOT EXISTS idx_workspace_members_ws
            ON workspace_members(workspace);

        -- --------------------------------------------- preset system
        -- (decisions 9/15): plugin_profiles folds to scope=global
        -- presets CARRYING their old ids (provenance references them).
        -- owner_id uses the '' sentinel for global (SQLite UNIQUE
        -- treats NULLs as distinct — '' keeps the scoped-unique
        -- constraint enforceable). version starts at 1: the immutable
        -- first version of every folded template.
        CREATE TABLE IF NOT EXISTS presets (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            preset_id      INTEGER NOT NULL,
            scope          TEXT NOT NULL DEFAULT 'global',
            owner_id       TEXT NOT NULL DEFAULT '',
            plugin         TEXT NOT NULL,
            name           TEXT NOT NULL,
            description    TEXT NOT NULL DEFAULT '',
            version        INTEGER NOT NULL DEFAULT 1,
            schema_version INTEGER NOT NULL DEFAULT 1,
            settings_json  TEXT NOT NULL DEFAULT '{}',
            archived       INTEGER NOT NULL DEFAULT 0,
            created_at     REAL NOT NULL,
            updated_at     REAL NOT NULL,
            UNIQUE (preset_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_presets_identity
            ON presets(scope, owner_id, plugin, name);
        INSERT INTO presets (preset_id, scope, owner_id, plugin, name,
                             description, version, schema_version,
                             settings_json, created_at, updated_at)
        SELECT id, 'global', '', plugin, name, description, 1, 1,
               settings_json, created_at, updated_at
        FROM plugin_profiles;

        -- provenance columns on the assignment row (decision 9/D.4:
        -- single source per pair in v1).
        ALTER TABLE account_plugins ADD COLUMN source_preset_id TEXT;
        ALTER TABLE account_plugins ADD COLUMN source_version INTEGER;
        ALTER TABLE account_plugins ADD COLUMN applied_at REAL;

        -- LIVE-BINDING MATERIALIZATION (§13.1): every bound pair's
        -- effective profile contribution becomes account-owned rows —
        -- keys the account layer already overrides stay untouched
        -- (precedence preserved byte-for-byte), the reserved
        -- "channels" key is EXCLUDED from settings materialization
        -- (it extracts into bindings below, not into settings).
        -- NOTE: SQLite's JSON layer stores true/false as integers;
        -- json_each's `type` column is what distinguishes them, so
        -- booleans materialize as `true`/`false` exactly like the
        -- Python-encoded account layer writes them (the §13.2
        -- effective-values invariant depends on this).
        INSERT INTO plugin_settings (account, plugin, key, value_json)
        SELECT ap.account, ap.plugin, pk.key,
               CASE WHEN pk.type = 'true' THEN 'true'
                    WHEN pk.type = 'false' THEN 'false'
                    ELSE json_quote(pk.value) END
        FROM account_plugins ap
        JOIN plugin_profiles pp
          ON pp.plugin = ap.plugin AND pp.name = ap.profile
        JOIN json_each(pp.settings_json) pk
        WHERE ap.profile IS NOT NULL
          AND pk.key != 'channels'
          AND NOT EXISTS (
              SELECT 1 FROM plugin_settings ps
              WHERE ps.account = ap.account
                AND ps.plugin = ap.plugin
                AND ps.key = pk.key
          );
        UPDATE account_plugins
        SET source_preset_id = CAST(pp.id AS TEXT),
            source_version = 1,
            applied_at = CAST(strftime('%s', 'now') AS REAL)
        FROM plugin_profiles pp
        WHERE account_plugins.profile IS NOT NULL
          AND pp.plugin = account_plugins.plugin
          AND pp.name = account_plugins.profile;

        -- ------------------------------------------------- channels
        -- (§5.1): registry / assignments / plugin bindings. The
        -- reserved "channels" set extracts from BOTH config layers it
        -- could live in (the bound profile and an explicit account
        -- override) into real binding rows — the plugin's WHERE,
        -- account-owned forever after (rules 1-2).
        CREATE TABLE IF NOT EXISTS account_channels (
            account          TEXT NOT NULL,
            guild_id         TEXT NOT NULL,
            channel_id       TEXT NOT NULL,
            name             TEXT NOT NULL DEFAULT '',
            validity         TEXT NOT NULL DEFAULT 'unknown',
            last_seen_at     REAL,
            validation_reason TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (account, guild_id, channel_id)
        );
        CREATE TABLE IF NOT EXISTS account_channel_assignments (
            account    TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            guild_id   TEXT NOT NULL DEFAULT '',
            added_at   REAL NOT NULL,
            PRIMARY KEY (account, channel_id)
        );
        CREATE TABLE IF NOT EXISTS account_plugin_channels (
            account    TEXT NOT NULL,
            plugin     TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            guild_id   TEXT NOT NULL DEFAULT '',
            bound_at   REAL NOT NULL,
            PRIMARY KEY (account, plugin, channel_id)
        );
        INSERT OR IGNORE INTO account_plugin_channels
            (account, plugin, channel_id, guild_id, bound_at)
        SELECT ap.account, ap.plugin, ch.value, '',
               CAST(strftime('%s', 'now') AS REAL)
        FROM account_plugins ap
        JOIN plugin_profiles pp
          ON pp.plugin = ap.plugin AND pp.name = ap.profile
        JOIN json_each(json_extract(pp.settings_json, '$.channels')) ch
        WHERE ap.profile IS NOT NULL
          AND json_type(pp.settings_json, '$.channels') = 'array';
        INSERT OR IGNORE INTO account_plugin_channels
            (account, plugin, channel_id, guild_id, bound_at)
        SELECT ps.account, ps.plugin, ch.value, '',
               CAST(strftime('%s', 'now') AS REAL)
        FROM plugin_settings ps
        JOIN json_each(json_extract(ps.value_json, '$')) ch
        WHERE ps.key = 'channels'
          AND json_type(ps.value_json, '$') = 'array';

        -- ------------------------------------------ orchestration
        -- tasks: fleet assignments fold (group_name -> workspace).
        CREATE TABLE IF NOT EXISTS workspace_tasks (
            id            TEXT PRIMARY KEY,
            workspace     TEXT NOT NULL,
            task_id       TEXT NOT NULL,
            account       TEXT,
            status        TEXT NOT NULL DEFAULT 'queued',
            priority      INTEGER NOT NULL DEFAULT 0,
            chain_run_id  TEXT,
            step          INTEGER,
            generation    INTEGER NOT NULL DEFAULT 1,
            error         TEXT,
            resume_policy TEXT NOT NULL DEFAULT '',
            started_at    REAL,
            stopped_at    REAL,
            version       INTEGER NOT NULL DEFAULT 1,
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workspace_tasks_ws_status
            ON workspace_tasks(workspace, status);
        INSERT INTO workspace_tasks (id, workspace, task_id, account, status,
                                     priority, generation, error, resume_policy,
                                     started_at, stopped_at, version,
                                     created_at, updated_at)
        SELECT id, group_name, task_id, account, status, priority,
               generation, error, resume_policy, started_at, stopped_at,
               version, created_at,
               COALESCE(stopped_at, started_at, created_at)
        FROM fleet_assignments;

        -- chains + runs + step executions (W4-ready, decision 14):
        -- definitions validate on save; runs capture immutable
        -- snapshots at creation.
        CREATE TABLE IF NOT EXISTS workspace_chains (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace        TEXT NOT NULL,
            name             TEXT NOT NULL,
            schema_version   INTEGER NOT NULL DEFAULT 1,
            revision         INTEGER NOT NULL DEFAULT 1,
            enabled          INTEGER NOT NULL DEFAULT 1,
            definition_json  TEXT NOT NULL DEFAULT '{}',
            created_at       REAL NOT NULL,
            updated_at       REAL NOT NULL,
            UNIQUE (workspace, name)
        );
        CREATE TABLE IF NOT EXISTS chain_runs (
            id               TEXT PRIMARY KEY,
            chain_id         INTEGER,
            workspace        TEXT NOT NULL,
            definition_json  TEXT NOT NULL DEFAULT '{}',
            status           TEXT NOT NULL DEFAULT 'running',
            triggered_by     TEXT NOT NULL DEFAULT '',
            created_at       REAL NOT NULL,
            finished_at      REAL
        );
        CREATE TABLE IF NOT EXISTS step_executions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      TEXT NOT NULL,
            position    INTEGER NOT NULL,
            attempt     INTEGER NOT NULL DEFAULT 1,
            target      TEXT NOT NULL DEFAULT '',
            outcome     TEXT NOT NULL DEFAULT '',
            error       TEXT NOT NULL DEFAULT '',
            started_at  REAL NOT NULL,
            finished_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_step_executions_run
            ON step_executions(run_id, position);

        -- signals outbox (decision 12): durable acceptance BEFORE
        -- acknowledgement; delivery state hangs off signal_id.
        CREATE TABLE IF NOT EXISTS signals (
            signal_id      TEXT PRIMARY KEY,
            account        TEXT NOT NULL,
            plugin         TEXT NOT NULL,
            name           TEXT NOT NULL,
            payload_json   TEXT NOT NULL DEFAULT '{}',
            correlation_id TEXT NOT NULL DEFAULT '',
            created_at     REAL NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            attempts       INTEGER NOT NULL DEFAULT 0,
            acked_at       REAL,
            last_error     TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_signals_state ON signals(delivery_state);

        -- cooldowns: scope values rename (group->workspace,
        -- account->member); records carry untouched.
        CREATE TABLE IF NOT EXISTS workspace_cooldowns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type  TEXT NOT NULL,
            scope_id    TEXT NOT NULL,
            reason      TEXT NOT NULL,
            started_at  REAL NOT NULL,
            expires_at  REAL NOT NULL,
            source      TEXT NOT NULL DEFAULT 'manual',
            cleared_at  REAL,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workspace_cooldowns_scope
            ON workspace_cooldowns(scope_type, scope_id);
        INSERT INTO workspace_cooldowns (scope_type, scope_id, reason,
                                         started_at, expires_at, source,
                                         cleared_at, created_at, updated_at)
        SELECT CASE scope_type WHEN 'group' THEN 'workspace'
                               WHEN 'account' THEN 'member'
                               ELSE scope_type END,
               scope_id, reason, started_at, expires_at, source,
               cleared_at,
               CAST(strftime('%s', 'now') AS REAL),
               CAST(strftime('%s', 'now') AS REAL)
        FROM fleet_cooldowns;

        -- transition history carries (renames only).
        CREATE TABLE IF NOT EXISTS workspace_transitions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            kind            TEXT NOT NULL,
            workspace       TEXT NOT NULL,
            generation      INTEGER NOT NULL,
            reason          TEXT NOT NULL,
            correlation_id  TEXT NOT NULL,
            occurred_at     REAL NOT NULL,
            task_id         TEXT,
            run_id          TEXT,
            account         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_workspace_transitions_ws
            ON workspace_transitions(workspace, id);
        INSERT INTO workspace_transitions (kind, workspace, generation, reason,
                                           correlation_id, occurred_at, task_id,
                                           run_id, account)
        SELECT kind, group_name, generation, reason, correlation_id,
               occurred_at, task_id, assignment_id, account
        FROM fleet_transitions;

        -- cross-store deletion workflow (decision 18): durable,
        -- retryable, observable — one job row per removed account.
        CREATE TABLE IF NOT EXISTS deletion_jobs (
            account      TEXT PRIMARY KEY,
            state        TEXT NOT NULL DEFAULT 'pending',
            attempts     INTEGER NOT NULL DEFAULT 0,
            last_error   TEXT NOT NULL DEFAULT '',
            created_at   REAL NOT NULL,
            completed_at REAL
        );
        """,
    ),
    (
        11,
        "workspace unification W5 — the v9 tables drop",
        """
        -- The readers are gone (W3 swapped the surfaces, W5 deleted the
        -- modules): the v9 orchestration tables die now. accounts.sync_group
        -- drops with them (accounts.workspace has been authoritative since
        -- v10). Requires SQLite >= 3.35 for DROP COLUMN (the fork's
        -- Python 3.10+ floor ships it).
        DROP TABLE IF EXISTS fleet_transitions;
        DROP TABLE IF EXISTS fleet_cooldowns;
        DROP TABLE IF EXISTS fleet_assignments;
        DROP TABLE IF EXISTS fleet_members;
        DROP TABLE IF EXISTS fleet_groups;
        DROP TABLE IF EXISTS sync_groups;
        ALTER TABLE accounts DROP COLUMN sync_group;
        """,
    ),
    (
        12,
        "two-level model P2 — account-owned plugin profiles",
        """
        -- The execution layer of the two-level data model
        -- (docs/design/data-management-adoption.md): each account owns
        -- its plugin profile INSTANCES. Distinct from the v7
        -- plugin_profiles table (the template-era bundle keyed by
        -- (plugin, name) with NO account dimension) — P7 migrates those
        -- rows into this shape; until then both tables coexist.
        -- settings_json is opaque to the core: the plugin owns its
        -- schema (schema_version lets the plugin evolve the shape).
        CREATE TABLE IF NOT EXISTS account_plugin_profiles (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            account        TEXT NOT NULL,
            plugin         TEXT NOT NULL,
            name           TEXT NOT NULL,
            description    TEXT NOT NULL DEFAULT '',
            settings_json  TEXT NOT NULL DEFAULT '{}',
            schema_version INTEGER NOT NULL DEFAULT 1,
            created_at     REAL NOT NULL,
            updated_at     REAL NOT NULL,
            UNIQUE(account, plugin, name)
        );
        CREATE INDEX IF NOT EXISTS idx_app_account_plugin
            ON account_plugin_profiles(account, plugin);
        """,
    ),
    (
        13,
        "two-level model P1 — cooldowns gain the profile dimension",
        """
        -- (account, plugin, PROFILE, cooldown_key) becomes the cooldown
        -- identity (docs/design/data-management-adoption.md): one
        -- profile's cooldown never blocks another profile's. '' is the
        -- documented profile-less scope — exactly the pre-P1 behavior,
        -- so existing rows migrate verbatim and every existing caller
        -- keeps its semantics. SQLite cannot ALTER a UNIQUE constraint,
        -- so this is the classic rebuild: new table, copy, drop, rename.
        CREATE TABLE cooldowns_v13 (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            account       TEXT NOT NULL,
            plugin        TEXT NOT NULL,
            profile       TEXT NOT NULL DEFAULT '',
            cooldown_key  TEXT NOT NULL,
            started_at    REAL NOT NULL,
            expires_at    REAL NOT NULL,
            source        TEXT NOT NULL DEFAULT '',
            reason        TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL,
            UNIQUE (account, plugin, profile, cooldown_key)
        );
        INSERT INTO cooldowns_v13 (account, plugin, profile, cooldown_key,
            started_at, expires_at, source, reason, metadata_json,
            created_at, updated_at)
            SELECT account, plugin, '', cooldown_key, started_at, expires_at,
                   source, reason, metadata_json, created_at, updated_at
            FROM cooldowns;
        DROP TABLE cooldowns;
        ALTER TABLE cooldowns_v13 RENAME TO cooldowns;
        """,
    ),
    (
        14,
        "two-level model P3 — the active-profile pointer",
        """
        -- Which account-owned profile is ACTIVE for one (account, plugin)
        -- (docs/design/data-management-adoption.md P3). One row per pair,
        -- updated in place; deleting the pointed-at profile clears the
        -- pointer (the repository coordinates both tables in one tx).
        -- Deliberately NOT the legacy account_plugins.profile column:
        -- that column names LEGACY template profiles until P7 folds
        -- them into account_plugin_profiles and repoints everything.
        CREATE TABLE IF NOT EXISTS account_plugin_active_profile (
            account    TEXT NOT NULL,
            plugin     TEXT NOT NULL,
            profile    TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (account, plugin)
        );
        """,
    ),
]


def latest_version() -> int:
    """Highest migration version defined (0 when none)."""
    return MIGRATIONS[-1][0] if MIGRATIONS else 0


__all__ = ["MIGRATIONS", "latest_version"]
