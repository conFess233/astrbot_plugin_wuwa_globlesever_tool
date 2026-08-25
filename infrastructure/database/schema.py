"""数据库历史基线与可复用建表语句。"""

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS users (
    qq_id TEXT PRIMARY KEY,
    language TEXT NOT NULL DEFAULT 'zh-CN',
    default_uid TEXT,
    active_profile_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    credential_id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq_id TEXT NOT NULL REFERENCES users(qq_id) ON DELETE CASCADE,
    account_identity_hmac TEXT NOT NULL UNIQUE,
    email_masked TEXT NOT NULL,
    encrypted_tokens TEXT NOT NULL,
    encrypted_device_id TEXT NOT NULL,
    token_status TEXT NOT NULL DEFAULT 'unknown',
    last_success_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS game_accounts (
    uid TEXT PRIMARY KEY,
    qq_id TEXT NOT NULL REFERENCES users(qq_id) ON DELETE CASCADE,
    credential_id INTEGER NOT NULL REFERENCES credentials(credential_id) ON DELETE CASCADE,
    region_id TEXT NOT NULL,
    region_name TEXT NOT NULL,
    player_name TEXT,
    sync_status TEXT NOT NULL DEFAULT 'never',
    last_sync_attempt_at TEXT,
    last_sync_success_at TEXT,
    last_error_category TEXT
);

CREATE TABLE IF NOT EXISTS profiles (
    profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq_id TEXT NOT NULL REFERENCES users(qq_id) ON DELETE CASCADE,
    profile_type TEXT NOT NULL CHECK (profile_type IN ('local', 'uid')),
    uid TEXT REFERENCES game_accounts(uid) ON DELETE CASCADE,
    updated_at TEXT NOT NULL,
    CHECK (
        (profile_type = 'local' AND uid IS NULL)
        OR (profile_type = 'uid' AND uid IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_profiles_local
ON profiles(qq_id) WHERE profile_type = 'local';

CREATE UNIQUE INDEX IF NOT EXISTS ux_profiles_uid
ON profiles(uid) WHERE profile_type = 'uid';

CREATE TABLE IF NOT EXISTS characters (
    profile_id INTEGER NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    character_id TEXT NOT NULL,
    character_name_snapshot TEXT NOT NULL,
    record_origin TEXT NOT NULL CHECK (record_origin IN ('api', 'manual', 'mixed')),
    api_owned INTEGER,
    api_level INTEGER,
    api_chain INTEGER,
    api_weapon_id TEXT,
    api_weapon_present INTEGER,
    manual_level INTEGER,
    manual_chain INTEGER,
    manual_weapon_id TEXT,
    manual_weapon_level INTEGER,
    manual_weapon_refinement INTEGER,
    score_total REAL,
    score_grade TEXT,
    score_provider TEXT,
    score_updated_at TEXT,
    score_status TEXT NOT NULL DEFAULT 'unavailable',
    last_api_sync_at TEXT,
    last_manual_update_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, character_id)
);

CREATE TABLE IF NOT EXISTS pending_logins (
    session_id TEXT PRIMARY KEY,
    requesting_qq_id TEXT NOT NULL,
    origin_context TEXT NOT NULL,
    link_token_hash TEXT NOT NULL UNIQUE,
    encrypted_pending_tokens TEXT,
    available_uids_json TEXT,
    selected_uids_json TEXT,
    selected_default_uid TEXT,
    confirm_code_hash TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_pending_logins_expires_at ON pending_logins(expires_at);

CREATE TABLE IF NOT EXISTS pending_actions (
    action_id TEXT PRIMARY KEY,
    qq_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    confirm_code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_pending_actions_expires_at ON pending_actions(expires_at);

CREATE TABLE IF NOT EXISTS admin_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_identity TEXT NOT NULL,
    action_type TEXT NOT NULL,
    masked_target TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_admin_audit_created_at ON admin_audit(created_at DESC);
"""

SCHEMA_V2_TABLES = """
CREATE INDEX IF NOT EXISTS ix_pending_logins_session_token_hash
ON pending_logins(session_token_hash);

CREATE TABLE IF NOT EXISTS login_rate_limits (
    scope TEXT NOT NULL,
    identity_hmac TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    window_started_at TEXT NOT NULL,
    blocked_until TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, identity_hmac)
);
"""

SCHEMA_V4_TABLES = """
CREATE TABLE IF NOT EXISTS player_snapshots (
    uid TEXT PRIMARY KEY REFERENCES game_accounts(uid) ON DELETE CASCADE,
    player_name TEXT,
    head_photo INTEGER,
    level INTEGER,
    world_level INTEGER,
    role_num INTEGER,
    active_days INTEGER,
    created_at_ms INTEGER,
    energy INTEGER,
    max_energy INTEGER,
    store_energy INTEGER,
    max_store_energy INTEGER,
    energy_recover_time_ms INTEGER,
    store_energy_recover_time_ms INTEGER,
    liveness INTEGER,
    liveness_max INTEGER,
    liveness_unlock INTEGER,
    weekly_inst_count INTEGER,
    sound_box INTEGER,
    boxes_json TEXT,
    basic_boxes_json TEXT,
    phantom_boxes_json TEXT,
    refreshed_at TEXT NOT NULL
);
"""

GAME_ACCOUNTS_V5 = """
CREATE TABLE game_accounts_v5 (
    region_id TEXT NOT NULL,
    uid TEXT NOT NULL,
    qq_id TEXT NOT NULL REFERENCES users(qq_id) ON DELETE CASCADE,
    credential_id INTEGER NOT NULL REFERENCES credentials(credential_id) ON DELETE CASCADE,
    region_name TEXT NOT NULL,
    player_name TEXT,
    sync_status TEXT NOT NULL DEFAULT 'never',
    last_sync_attempt_at TEXT,
    last_sync_success_at TEXT,
    last_error_category TEXT,
    PRIMARY KEY (region_id, uid)
)
"""

PROFILES_V5 = """
CREATE TABLE profiles_v5 (
    profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq_id TEXT NOT NULL REFERENCES users(qq_id) ON DELETE CASCADE,
    profile_type TEXT NOT NULL CHECK (profile_type IN ('local', 'uid')),
    region_id TEXT,
    uid TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (region_id, uid)
        REFERENCES game_accounts_v5(region_id, uid) ON DELETE CASCADE,
    CHECK (
        (profile_type = 'local' AND region_id IS NULL AND uid IS NULL)
        OR (profile_type = 'uid' AND region_id IS NOT NULL AND uid IS NOT NULL)
    )
)
"""

CHARACTERS_V5 = """
CREATE TABLE characters_v5 (
    profile_id INTEGER NOT NULL REFERENCES profiles_v5(profile_id) ON DELETE CASCADE,
    character_id TEXT NOT NULL,
    character_name_snapshot TEXT NOT NULL,
    record_origin TEXT NOT NULL CHECK (record_origin IN ('api', 'manual', 'mixed')),
    api_owned INTEGER,
    api_level INTEGER,
    api_chain INTEGER,
    api_weapon_id TEXT,
    api_weapon_present INTEGER,
    manual_level INTEGER,
    manual_chain INTEGER,
    manual_weapon_id TEXT,
    manual_weapon_level INTEGER,
    manual_weapon_refinement INTEGER,
    score_total REAL,
    score_grade TEXT,
    score_provider TEXT,
    score_updated_at TEXT,
    score_status TEXT NOT NULL DEFAULT 'unavailable',
    last_api_sync_at TEXT,
    last_manual_update_at TEXT,
    updated_at TEXT NOT NULL,
    api_source_order INTEGER,
    api_weapon_name TEXT,
    api_weapon_picture_url TEXT,
    api_weapon_star INTEGER,
    api_weapon_type_id TEXT,
    api_weapon_type_picture_url TEXT,
    PRIMARY KEY (profile_id, character_id)
)
"""

PLAYER_SNAPSHOTS_V5 = """
CREATE TABLE player_snapshots_v5 (
    region_id TEXT NOT NULL,
    uid TEXT NOT NULL,
    player_name TEXT,
    head_photo INTEGER,
    level INTEGER,
    world_level INTEGER,
    role_num INTEGER,
    active_days INTEGER,
    created_at_ms INTEGER,
    energy INTEGER,
    max_energy INTEGER,
    store_energy INTEGER,
    max_store_energy INTEGER,
    energy_recover_time_ms INTEGER,
    store_energy_recover_time_ms INTEGER,
    liveness INTEGER,
    liveness_max INTEGER,
    liveness_unlock INTEGER,
    weekly_inst_count INTEGER,
    sound_box INTEGER,
    boxes_json TEXT,
    basic_boxes_json TEXT,
    phantom_boxes_json TEXT,
    refreshed_at TEXT NOT NULL,
    PRIMARY KEY (region_id, uid),
    FOREIGN KEY (region_id, uid)
        REFERENCES game_accounts_v5(region_id, uid) ON DELETE CASCADE
)
"""
