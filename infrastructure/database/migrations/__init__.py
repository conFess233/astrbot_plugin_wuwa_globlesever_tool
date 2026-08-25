"""按版本显式注册的 SQLite 迁移。"""

import sqlite3

from .v001_initial import migrate as migrate_v1
from .v002_login_security import migrate as migrate_v2
from .v003_source_order import migrate as migrate_v3
from .v004_player_snapshot import migrate as migrate_v4
from .v005_region_uid_redesign import migrate as migrate_v5

_MIGRATIONS = {
    1: migrate_v1,
    2: migrate_v2,
    3: migrate_v3,
    4: migrate_v4,
    5: migrate_v5,
}


def apply_migration(connection: sqlite3.Connection, target_version: int) -> None:
    try:
        migration = _MIGRATIONS[target_version]
    except KeyError as exc:
        raise RuntimeError(f"缺少数据库迁移版本 {target_version}") from exc
    migration(connection)
