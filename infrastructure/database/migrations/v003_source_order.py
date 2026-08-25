"""为角色快照补充攻略站顺序。"""

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(characters)").fetchall()
    }
    if "api_source_order" not in existing:
        connection.execute("ALTER TABLE characters ADD COLUMN api_source_order INTEGER")
