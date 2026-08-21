"""SQLite 数据库结构和增量迁移入口。"""

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from ..constants import SCHEMA_VERSION

_T = TypeVar("_T")

_SCHEMA_V1 = """
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

_SCHEMA_V2_TABLES = """
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

_SCHEMA_V3 = """
ALTER TABLE characters ADD COLUMN api_source_order INTEGER;
"""


class DatabaseError(RuntimeError):
    """表示数据库版本或初始化失败。"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._initialized = False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)
        self._initialized = True

    async def health(self) -> dict[str, Any]:
        if not self._initialized:
            return {"initialized": False, "schema_version": None}
        return await asyncio.to_thread(self._health_sync)

    async def close(self) -> None:
        self._initialized = False

    async def read(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        if not self._initialized:
            raise DatabaseError("数据库尚未初始化")
        return await asyncio.to_thread(self._run_sync, operation, False)

    async def write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        if not self._initialized:
            raise DatabaseError("数据库尚未初始化")
        return await asyncio.to_thread(self._run_sync, operation, True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _run_sync(
        self,
        operation: Callable[[sqlite3.Connection], _T],
        write: bool,
    ) -> _T:
        connection = self._connect()
        try:
            if write:
                with connection:
                    return operation(connection)
            return operation(connection)
        finally:
            connection.close()

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            with connection:
                connection.execute("PRAGMA journal_mode = WAL")
                current = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if current > SCHEMA_VERSION:
                    raise DatabaseError(f"数据库版本 {current} 高于插件支持版本 {SCHEMA_VERSION}")
                if current == 0:
                    connection.executescript(_SCHEMA_V1)
                    connection.execute("PRAGMA user_version = 1")
                    current = 1
                if current == 1:
                    self._migrate_v2(connection)
                    connection.execute("PRAGMA user_version = 2")
                    current = 2
                if current == 2:
                    self._migrate_v3(connection)
                    connection.execute("PRAGMA user_version = 3")
                    current = 3
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise DatabaseError("数据库外键检查失败")
        finally:
            connection.close()

    @staticmethod
    def _migrate_v2(connection: sqlite3.Connection) -> None:
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
        connection.executescript(_SCHEMA_V2_TABLES)

    @staticmethod
    def _migrate_v3(connection: sqlite3.Connection) -> None:
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        }
        if "api_source_order" not in existing:
            connection.executescript(_SCHEMA_V3)

    def _health_sync(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            return {
                "initialized": True,
                "schema_version": schema_version,
                "integrity": integrity,
            }
        finally:
            connection.close()
