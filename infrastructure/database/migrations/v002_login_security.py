"""为网页登录安全状态补充 v2 字段。"""

import sqlite3

from ..schema import SCHEMA_V2_TABLES
from ._utils import execute_script


def migrate(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(pending_logins)").fetchall()
    }
    columns = {
        "status": "TEXT NOT NULL DEFAULT 'created'",
        "session_token_hash": "TEXT",
        "csrf_token_hash": "TEXT",
        "link_used_at": "TEXT",
        "email_identity_hmac": "TEXT",
        "email_masked": "TEXT",
        "updated_at": "TEXT",
    }
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE pending_logins ADD COLUMN {name} {definition}")
    execute_script(connection, SCHEMA_V2_TABLES)
