"""攻略站同步跨层使用的稳定模型。"""

from dataclasses import dataclass
from typing import Protocol


class GuideError(RuntimeError):
    """表示攻略站拒绝请求或返回无效数据。"""


class GuideAuthenticationError(GuideError):
    """表示攻略站登录状态失效。"""


class GuideUnavailableError(GuideError):
    """表示攻略站网络或服务暂时不可用。"""


@dataclass(frozen=True, slots=True)
class GuideSyncPlayer:
    uid: str
    region_id: str
    region_name: str
    player_name: str | None


@dataclass(frozen=True, slots=True)
class GuideAvatar:
    role_id: str
    is_acquired: bool
    source_order: int


@dataclass(frozen=True, slots=True)
class GuideIntroduction:
    introduction_id: int
    languages: tuple[str, ...]
    modified_at: int | None


@dataclass(frozen=True, slots=True)
class GuideRoleDetail:
    chain: int | None
    weapon_present: bool | None
    weapon_id: str | None
    weapon_name: str | None
    weapon_picture_url: str | None
    weapon_star: int | None
    weapon_type_id: str | None
    weapon_type_picture_url: str | None


class GuideSyncClient(Protocol):
    async def login(self, c_uid: str, c_name: str, access_token: str, language: str) -> str: ...

    async def players(self, token: str, language: str) -> tuple[GuideSyncPlayer, ...]: ...

    async def choose_player(self, token: str, language: str, uid: str, region_id: str) -> None: ...

    async def avatars(self, token: str, language: str) -> tuple[GuideAvatar, ...]: ...

    async def introductions(
        self, token: str, language: str, role_id: str
    ) -> tuple[GuideIntroduction, ...]: ...

    async def introduction_detail(
        self, token: str, language: str, role_id: str, introduction_id: int
    ) -> GuideRoleDetail | None: ...


@dataclass(frozen=True, slots=True)
class SyncedCharacter:
    role_id: str
    role_name: str
    chain: int | None
    weapon_present: bool | None
    weapon_id: str | None
    weapon_name: str | None
    weapon_picture_url: str | None
    weapon_star: int | None
    weapon_type_id: str | None
    weapon_type_picture_url: str | None
    source_order: int


@dataclass(frozen=True, slots=True)
class SyncResult:
    uid: str
    region_id: str
    owned_count: int
    synced_at: str
