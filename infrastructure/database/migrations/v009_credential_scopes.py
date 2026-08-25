"""将游戏接口与攻略站凭据状态拆分为独立生命周期。"""

import sqlite3

from ._utils import column_names


def migrate(connection: sqlite3.Connection) -> None:
    columns = column_names(connection, "credentials")
    if "game_token_status" not in columns:
        connection.execute(
            "ALTER TABLE credentials ADD COLUMN game_token_status TEXT NOT NULL "
            "DEFAULT 'unknown' CHECK (game_token_status IN "
            "('unknown', 'valid', 'needs_login', 'invalid'))"
        )
    if "guide_token_status" not in columns:
        connection.execute(
            "ALTER TABLE credentials ADD COLUMN guide_token_status TEXT NOT NULL "
            "DEFAULT 'unknown' CHECK (guide_token_status IN "
            "('unknown', 'valid', 'needs_login', 'invalid'))"
        )
    connection.execute(
        "UPDATE credentials SET game_token_status = 'unknown', guide_token_status = 'unknown'"
    )
