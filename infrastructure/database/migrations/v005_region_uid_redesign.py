"""把 UID 单键无损升级为 (region_id, uid) 复合账号键。"""

import sqlite3

from ..schema import CHARACTERS_V5, GAME_ACCOUNTS_V5, PLAYER_SNAPSHOTS_V5, PROFILES_V5

_CHARACTER_COLUMNS = (
    "profile_id",
    "character_id",
    "character_name_snapshot",
    "record_origin",
    "api_owned",
    "api_level",
    "api_chain",
    "api_weapon_id",
    "api_weapon_present",
    "manual_level",
    "manual_chain",
    "manual_weapon_id",
    "manual_weapon_level",
    "manual_weapon_refinement",
    "score_total",
    "score_grade",
    "score_provider",
    "score_updated_at",
    "score_status",
    "last_api_sync_at",
    "last_manual_update_at",
    "updated_at",
    "api_source_order",
    "api_weapon_name",
    "api_weapon_picture_url",
    "api_weapon_star",
    "api_weapon_type_id",
    "api_weapon_type_picture_url",
)

_SNAPSHOT_COLUMNS = (
    "uid",
    "player_name",
    "head_photo",
    "level",
    "world_level",
    "role_num",
    "active_days",
    "created_at_ms",
    "energy",
    "max_energy",
    "store_energy",
    "max_store_energy",
    "energy_recover_time_ms",
    "store_energy_recover_time_ms",
    "liveness",
    "liveness_max",
    "liveness_unlock",
    "weekly_inst_count",
    "sound_box",
    "boxes_json",
    "basic_boxes_json",
    "phantom_boxes_json",
    "refreshed_at",
)


def migrate(connection: sqlite3.Connection) -> None:
    _add_column(connection, "users", "default_region_id", "TEXT")
    _add_column(connection, "pending_logins", "available_accounts_json", "TEXT")
    _add_column(connection, "pending_logins", "selected_accounts_json", "TEXT")
    _add_column(connection, "pending_logins", "selected_default_region_id", "TEXT")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS migration_reports ("
        "report_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "schema_version INTEGER NOT NULL, category TEXT NOT NULL, "
        "detail TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )

    connection.execute(GAME_ACCOUNTS_V5)
    connection.execute(PROFILES_V5)
    connection.execute(CHARACTERS_V5)
    connection.execute(PLAYER_SNAPSHOTS_V5)

    _record_skipped(
        connection,
        "account_region_missing",
        connection.execute(
            "SELECT COUNT(*) FROM game_accounts WHERE region_id IS NULL OR TRIM(region_id) = ''"
        ).fetchone()[0],
    )

    connection.execute(
        "INSERT INTO game_accounts_v5 "
        "(region_id, uid, qq_id, credential_id, region_name, player_name, sync_status, "
        "last_sync_attempt_at, last_sync_success_at, last_error_category) "
        "SELECT region_id, uid, qq_id, credential_id, region_name, player_name, sync_status, "
        "last_sync_attempt_at, last_sync_success_at, last_error_category FROM game_accounts "
        "WHERE region_id IS NOT NULL AND TRIM(region_id) <> ''"
    )
    _record_skipped(
        connection,
        "profile_account_missing",
        connection.execute(
            "SELECT COUNT(*) FROM profiles p "
            "WHERE p.profile_type = 'uid' AND NOT EXISTS ("
            "SELECT 1 FROM game_accounts_v5 g WHERE g.uid = p.uid)"
        ).fetchone()[0],
    )
    connection.execute(
        "INSERT INTO profiles_v5 "
        "(profile_id, qq_id, profile_type, region_id, uid, updated_at) "
        "SELECT p.profile_id, p.qq_id, p.profile_type, NULL, NULL, p.updated_at "
        "FROM profiles p WHERE p.profile_type = 'local' "
        "UNION ALL "
        "SELECT p.profile_id, p.qq_id, p.profile_type, g.region_id, p.uid, p.updated_at "
        "FROM profiles p JOIN game_accounts_v5 g ON g.uid = p.uid "
        "WHERE p.profile_type = 'uid'"
    )

    _record_skipped(
        connection,
        "character_profile_missing",
        connection.execute(
            "SELECT COUNT(*) FROM characters c WHERE NOT EXISTS ("
            "SELECT 1 FROM profiles_v5 p WHERE p.profile_id = c.profile_id)"
        ).fetchone()[0],
    )
    character_columns = ", ".join(_CHARACTER_COLUMNS)
    connection.execute(
        f"INSERT INTO characters_v5 ({character_columns}) "
        f"SELECT {', '.join(f'c.{column}' for column in _CHARACTER_COLUMNS)} "
        "FROM characters c JOIN profiles_v5 p ON p.profile_id = c.profile_id"
    )
    _record_skipped(
        connection,
        "snapshot_account_missing",
        connection.execute(
            "SELECT COUNT(*) FROM player_snapshots s WHERE NOT EXISTS ("
            "SELECT 1 FROM game_accounts_v5 g WHERE g.uid = s.uid)"
        ).fetchone()[0],
    )
    snapshot_columns = ", ".join(_SNAPSHOT_COLUMNS)
    connection.execute(
        "INSERT INTO player_snapshots_v5 (region_id, "
        f"{snapshot_columns}) "
        "SELECT g.region_id, "
        + ", ".join(f"s.{column}" for column in _SNAPSHOT_COLUMNS)
        + " FROM player_snapshots s JOIN game_accounts_v5 g ON g.uid = s.uid"
    )

    connection.execute("DROP TABLE player_snapshots")
    connection.execute("DROP TABLE characters")
    connection.execute("DROP TABLE profiles")
    connection.execute("DROP TABLE game_accounts")

    connection.execute("ALTER TABLE game_accounts_v5 RENAME TO game_accounts")
    connection.execute("ALTER TABLE profiles_v5 RENAME TO profiles")
    connection.execute("ALTER TABLE characters_v5 RENAME TO characters")
    connection.execute("ALTER TABLE player_snapshots_v5 RENAME TO player_snapshots")

    connection.execute(
        "CREATE UNIQUE INDEX ux_profiles_local ON profiles(qq_id) WHERE profile_type = 'local'"
    )
    connection.execute(
        "CREATE UNIQUE INDEX ux_profiles_region_uid ON profiles(region_id, uid) "
        "WHERE profile_type = 'uid'"
    )
    connection.execute("CREATE INDEX ix_game_accounts_qq ON game_accounts(qq_id, region_id, uid)")
    connection.execute("CREATE INDEX ix_game_accounts_credential ON game_accounts(credential_id)")
    connection.execute(
        "UPDATE users SET default_region_id = ("
        "SELECT g.region_id FROM game_accounts g "
        "WHERE g.qq_id = users.qq_id AND g.uid = users.default_uid LIMIT 1"
        ") WHERE default_uid IS NOT NULL"
    )
    connection.execute(
        "UPDATE users SET default_uid = NULL "
        "WHERE default_uid IS NOT NULL AND default_region_id IS NULL"
    )
    connection.execute(
        "UPDATE users SET active_profile_id = ("
        "SELECT p.profile_id FROM profiles p "
        "WHERE p.qq_id = users.qq_id AND p.profile_type = 'local' LIMIT 1"
        ") WHERE active_profile_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM profiles p WHERE p.profile_id = users.active_profile_id)"
    )


def _add_column(
    connection: sqlite3.Connection,
    table: str,
    name: str,
    definition: str,
) -> None:
    existing = {
        str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if name not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _record_skipped(connection: sqlite3.Connection, category: str, count: int) -> None:
    if count <= 0:
        return
    connection.execute(
        "INSERT INTO migration_reports (schema_version, category, detail) VALUES (5, ?, ?)",
        (category, f"迁移时跳过 {count} 行无法安全归属的数据"),
    )
