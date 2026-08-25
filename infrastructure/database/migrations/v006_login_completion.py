"""补充网页直接完成绑定所需的会话与凭据状态。"""

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "users",
        {
            "last_origin_context": "TEXT",
        },
    )
    _add_columns(
        connection,
        "credentials",
        {
            "expires_at": "TEXT",
            "revoked_at": "TEXT",
            "notification_suppressed_until": "TEXT",
        },
    )
    _add_columns(
        connection,
        "game_accounts",
        {
            "bound_at": "TEXT",
        },
    )
    _add_columns(
        connection,
        "pending_logins",
        {
            "link_exchanged_at": "TEXT",
            "completed_at": "TEXT",
            "locked_until": "TEXT",
            "last_client_ip_hmac": "TEXT",
        },
    )
    connection.execute(
        "UPDATE game_accounts SET bound_at = COALESCE(bound_at, last_sync_success_at, "
        "last_sync_attempt_at, CURRENT_TIMESTAMP)"
    )


def _add_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {
        str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
