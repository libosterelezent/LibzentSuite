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
]


def latest_version() -> int:
    """Highest migration version defined (0 when none)."""
    return MIGRATIONS[-1][0] if MIGRATIONS else 0


__all__ = ["MIGRATIONS", "latest_version"]
