"""创建历史 v1 基线，后续迁移负责升级到当前结构。"""

import sqlite3

from ..schema import SCHEMA_V1
from ._utils import execute_script


def migrate(connection: sqlite3.Connection) -> None:
    execute_script(connection, SCHEMA_V1)
