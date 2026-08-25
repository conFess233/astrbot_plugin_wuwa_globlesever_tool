"""增加刷新状态与会话绑定的两步确认字段。"""

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(pending_actions)").fetchall()
    }
    if "origin_context" not in columns:
        connection.execute("ALTER TABLE pending_actions ADD COLUMN origin_context TEXT")
    if "summary" not in columns:
        connection.execute("ALTER TABLE pending_actions ADD COLUMN summary TEXT")
    player_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(player_snapshots)").fetchall()
    }
    battle_pass_columns = {
        "battle_pass_present": "INTEGER",
        "battle_pass_level": "INTEGER",
        "battle_pass_week_exp": "INTEGER",
        "battle_pass_week_max_exp": "INTEGER",
        "battle_pass_is_unlock": "INTEGER",
        "battle_pass_is_open": "INTEGER",
        "battle_pass_exp": "INTEGER",
        "battle_pass_exp_limit": "INTEGER",
    }
    for name, definition in battle_pass_columns.items():
        if name not in player_columns:
            connection.execute(f"ALTER TABLE player_snapshots ADD COLUMN {name} {definition}")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS refresh_states ("
        "refresh_kind TEXT NOT NULL CHECK (refresh_kind IN ('player', 'role')), "
        "region_id TEXT NOT NULL, uid TEXT NOT NULL, "
        "last_user_refresh_at TEXT, last_attempt_at TEXT, last_success_at TEXT, "
        "failure_count INTEGER NOT NULL DEFAULT 0, backoff_until TEXT, "
        "last_error_category TEXT, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (refresh_kind, region_id, uid), "
        "FOREIGN KEY (region_id, uid) REFERENCES game_accounts(region_id, uid) "
        "ON DELETE CASCADE)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_refresh_states_due "
        "ON refresh_states(refresh_kind, backoff_until, last_success_at)"
    )
