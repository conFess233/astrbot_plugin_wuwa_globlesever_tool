"""数据库和应用层共用的稳定枚举。"""

from enum import StrEnum


class ProfileType(StrEnum):
    LOCAL = "local"
    UID = "uid"


class RecordOrigin(StrEnum):
    API = "api"
    MANUAL = "manual"
    MIXED = "mixed"


class SyncStatus(StrEnum):
    NEVER = "never"
    SUCCESS = "success"
    FAILED = "failed"
    NEEDS_LOGIN = "needs_login"


class ScoreStatus(StrEnum):
    UNAVAILABLE = "unavailable"
