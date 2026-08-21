"""网页登录与账号绑定跨层传递的稳定数据结构。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class AuthenticationError(RuntimeError):
    """表示登录失败，并仅携带可安全展示的稳定分类。"""


class AuthenticationUnavailableError(AuthenticationError):
    """表示上游网络或响应格式暂时不可用。"""


@dataclass(frozen=True, slots=True)
class GeetestChallenge:
    captcha_id: str = "1f4565ff7acc97b1a2fc97b921743aa4"


@dataclass(frozen=True, slots=True)
class SdkLoginResult:
    risk_required: bool
    code: str | None = None
    c_uid: str | None = None
    c_name: str | None = None
    auto_token: str | None = None
    challenge: GeetestChallenge | None = None


@dataclass(frozen=True, slots=True)
class GuidePlayer:
    uid: str
    player_name: str | None
    region_id: str
    region_name: str
    level: int | None


@dataclass(frozen=True, slots=True)
class AuthenticatedAccount:
    c_uid: str
    c_name: str
    auto_token: str | None
    access_token: str
    oauth_code: str
    guide_token: str
    device_id: str
    players: tuple[GuidePlayer, ...]

    def sensitive_payload(self) -> dict[str, str | None]:
        return {
            "c_uid": self.c_uid,
            "c_name": self.c_name,
            "auto_token": self.auto_token,
            "access_token": self.access_token,
            "oauth_code": self.oauth_code,
            "guide_token": self.guide_token,
        }


class AuthClient(Protocol):
    @staticmethod
    def new_device_id() -> str: ...

    async def email_login(
        self,
        email: str,
        password: str,
        device_id: str,
        geetest: dict[str, str] | None = None,
    ) -> SdkLoginResult: ...

    async def complete_login(
        self,
        result: SdkLoginResult,
        device_id: str,
        language: str = "zh-Hans",
    ) -> AuthenticatedAccount: ...


@dataclass(frozen=True, slots=True)
class LoginLinkMessage:
    url: str
    expires_minutes: int


@dataclass(frozen=True, slots=True)
class BrowserSession:
    session_token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LoginSubmitResult:
    risk_required: bool
    captcha_id: str | None = None
    players: tuple[GuidePlayer, ...] = ()


@dataclass(frozen=True, slots=True)
class LoginSelectionResult:
    confirmation_code: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LoginConfirmationResult:
    email_masked: str
    selected_uids: tuple[str, ...]
    default_uid: str
