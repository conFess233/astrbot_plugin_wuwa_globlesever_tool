"""Dashboard 管理接口与公开的短期登录页接口。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from astrbot.api.web import error_response, file_response, json_response, request

from constants import PLUGIN_NAME, PLUGIN_VERSION
from infrastructure.database import Database
from infrastructure.http import HttpClient
from infrastructure.paths import RuntimePaths
from services.login_sessions import LoginSessionError, LoginSessionService
from services.settings import PluginSettings

_LOGIN_PAGE = Path(__file__).with_name("static") / "login.html"


class WebManager:
    def __init__(
        self,
        paths: RuntimePaths,
        database: Database,
        http: HttpClient,
        initialized: Callable[[], bool],
        settings: PluginSettings,
        login_sessions: Callable[[], LoginSessionService | None],
    ):
        self.paths = paths
        self.database = database
        self.http = http
        self.initialized = initialized
        self.settings = settings
        self.login_sessions = login_sessions

    @staticmethod
    def _authenticated() -> bool:
        return bool(request.username)

    async def health(self):
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        database = await self.database.health()
        payload: dict[str, Any] = {
            "plugin": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "initialized": self.initialized(),
            "database": database,
            "http": self.http.status(),
            "storage_root": str(self.paths.root),
        }
        return json_response(payload)

    async def login_page(self, link_token: str):
        service = self.login_sessions()
        if service is None or not await service.validate_link(link_token):
            return error_response("登录链接无效或已过期", status_code=410)
        return file_response(_LOGIN_PAGE, content_type="text/html; charset=utf-8")

    async def login_session_page(self):
        if self.login_sessions() is None:
            return error_response("登录服务尚未初始化", status_code=503)
        return file_response(_LOGIN_PAGE, content_type="text/html; charset=utf-8")

    async def login_exchange(self):
        blocked = self._public_post_guard()
        if blocked is not None:
            return blocked
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                raise LoginSessionError("请求格式无效")
            service = self._required_login_service()
            session = await service.exchange_link(str(payload.get("link_token") or ""))
            return json_response(
                {
                    "session_token": session.session_token,
                    "csrf_token": session.csrf_token,
                    "expires_at": session.expires_at.isoformat(),
                }
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    async def login_submit(self):
        blocked = self._public_post_guard()
        if blocked is not None:
            return blocked
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                raise LoginSessionError("请求格式无效")
            service = self._required_login_service()
            result = await service.submit_credentials(
                str(payload.get("session_token") or ""),
                str(payload.get("csrf_token") or ""),
                str(payload.get("email") or ""),
                str(payload.get("password") or ""),
                self._origin(),
                str(request.client_host or ""),
                payload.get("geetest") if isinstance(payload.get("geetest"), dict) else None,
            )
            if result.risk_required:
                return json_response(
                    {"status": "risk", "captcha_id": result.captcha_id},
                )
            return json_response(
                {
                    "status": "players",
                    "players": [
                        {
                            "uid": player.uid,
                            "player_name": player.player_name,
                            "region_id": player.region_id,
                            "region_name": player.region_name,
                            "level": player.level,
                        }
                        for player in result.players
                    ],
                }
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    async def login_select(self):
        blocked = self._public_post_guard()
        if blocked is not None:
            return blocked
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict) or not isinstance(payload.get("selected_uids"), list):
                raise LoginSessionError("请求格式无效")
            service = self._required_login_service()
            result = await service.select_uids(
                str(payload.get("session_token") or ""),
                str(payload.get("csrf_token") or ""),
                self._origin(),
                [str(uid) for uid in payload["selected_uids"]],
                str(payload.get("default_uid") or ""),
            )
            return json_response(
                {
                    "status": "awaiting_confirm",
                    "confirmation_code": result.confirmation_code,
                    "expires_at": result.expires_at.isoformat(),
                }
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    def _public_post_guard(self):
        if self._origin() != self.settings.public_https_base_url:
            return error_response("登录页面来源校验失败", status_code=403)
        content_type = str(request.headers.get("Content-Type") or "").split(";", 1)[0]
        if content_type.casefold() != "application/json":
            return error_response("仅接受 JSON 请求", status_code=415)
        return None

    @staticmethod
    def _origin() -> str:
        return str(request.headers.get("Origin") or "").rstrip("/")

    def _required_login_service(self) -> LoginSessionService:
        service = self.login_sessions()
        if service is None:
            raise LoginSessionError("登录服务尚未初始化")
        return service
