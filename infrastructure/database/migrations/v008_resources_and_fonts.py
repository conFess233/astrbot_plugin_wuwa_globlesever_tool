"""登记可变图片资源、字体与管理员自定义别名。"""

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS resource_entries ("
        "resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, "
        "source_url TEXT NOT NULL, cache_path TEXT NOT NULL, "
        "version TEXT, size_bytes INTEGER NOT NULL, "
        "status TEXT NOT NULL CHECK (status IN ('ready', 'stale', 'failed')), "
        "referenced INTEGER NOT NULL DEFAULT 0, "
        "last_accessed_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "metadata_json TEXT NOT NULL DEFAULT '{}', "
        "PRIMARY KEY (resource_type, resource_id))"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_resource_entries_lru "
        "ON resource_entries(referenced, last_accessed_at)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS font_entries ("
        "font_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
        "source_url TEXT, font_path TEXT NOT NULL UNIQUE, "
        "weight INTEGER NOT NULL DEFAULT 400, style TEXT NOT NULL DEFAULT 'normal', "
        "is_default INTEGER NOT NULL DEFAULT 0, installed_at TEXT NOT NULL, "
        "metadata_json TEXT NOT NULL DEFAULT '{}')"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_font_entries_default "
        "ON font_entries(is_default) WHERE is_default = 1"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS custom_aliases ("
        "alias_normalized TEXT PRIMARY KEY, alias_display TEXT NOT NULL, "
        "target_type TEXT NOT NULL CHECK (target_type IN ('character', 'weapon')), "
        "target_id TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
