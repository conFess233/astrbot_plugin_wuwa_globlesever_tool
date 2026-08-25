"""SQLite 数据库连接与事务化迁移入口。"""

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from ...constants import SCHEMA_VERSION
from .migrations import apply_migration

_T = TypeVar("_T")


class DatabaseError(RuntimeError):
    """表示数据库版本、完整性或初始化失败。"""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    from_version: int
    to_version: int

    @property
    def applied(self) -> bool:
        return self.from_version != self.to_version


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._initialized = False
        self.last_migration = MigrationResult(SCHEMA_VERSION, SCHEMA_VERSION)

    async def initialize(self) -> MigrationResult:
        result = await asyncio.to_thread(self._initialize_sync)
        self.last_migration = result
        self._initialized = True
        return result

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

    def _initialize_sync(self) -> MigrationResult:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            original = current
            if current > SCHEMA_VERSION:
                raise DatabaseError(f"数据库版本 {current} 高于插件支持版本 {SCHEMA_VERSION}")
            connection.execute("BEGIN IMMEDIATE")
            try:
                while current < SCHEMA_VERSION:
                    target = current + 1
                    apply_migration(connection, target)
                    connection.execute(f"PRAGMA user_version = {target}")
                    current = target
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise DatabaseError("数据库外键检查失败")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return MigrationResult(original, current)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError("数据库迁移失败，已回滚现有数据") from exc
        finally:
            connection.close()

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
