"""不会隐式提交事务的简单 SQL 脚本执行器。"""

import sqlite3


def column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    """读取指定表的现有列名，供可重复执行的迁移判断使用。"""

    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def execute_script(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        sql = statement.strip()
        if sql:
            connection.execute(sql)
