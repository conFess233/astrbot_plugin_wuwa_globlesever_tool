"""不会隐式提交事务的简单 SQL 脚本执行器。"""

import sqlite3


def execute_script(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        sql = statement.strip()
        if sql:
            connection.execute(sql)
