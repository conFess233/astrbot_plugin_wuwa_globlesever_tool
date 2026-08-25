"""补充武器展示字段和唯一玩家快照。"""

import sqlite3

from ..schema import SCHEMA_V4_TABLES
from ._utils import execute_script


def migrate(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(characters)").fetchall()
    }
    columns = {
        "api_weapon_name": "TEXT",
        "api_weapon_picture_url": "TEXT",
        "api_weapon_star": "INTEGER",
        "api_weapon_type_id": "TEXT",
        "api_weapon_type_picture_url": "TEXT",
    }
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE characters ADD COLUMN {name} {definition}")
    execute_script(connection, SCHEMA_V4_TABLES)
