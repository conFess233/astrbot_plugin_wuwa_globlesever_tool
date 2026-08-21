"""独立于 AstrBot Dashboard 鉴权的短期登录 HTTP 服务。"""

import asyncio
import ipaddress
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from ..constants import PUBLIC_LOGIN_PREFIX
from ..services.login_sessions import LoginSessionError, LoginSessionService
from ..services.settings import PluginSettings

_LOGIN_PAGE = Path(__file__).with_name("static") / "login.html"
_MAX_JSON_BYTES = 64 * 1024


class PublicLoginServerError(ValueError):
    """表示独立登录监听器无法按当前配置启动。"""


class PublicLoginRequestError(LoginSessionError):
    """表示公开登录请求的传输层校验失败。"""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class PublicLoginServer:
    """只暴露登录所需路由，不共享 AstrBot Dashboard 的 API Key。"""

    def __init__(
        self,
        settings: PluginSettings,
        login_sessions: Callable[[], LoginSessionService | None],
    ) -> None:
        self.settings = settings
        self.login_sessions = login_sessions
        self._runner: web.AppRunner | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._runner is not None

    async def start(self) -> None:
        async with self._lock:
            await self._start_unlocked()

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    async def update_settings(self, settings: PluginSettings) -> None:
        async with self._lock:
            old_settings = self.settings
            old_listener = self._listener_key(old_settings)
            new_listener = self._listener_key(settings)
            self.settings = settings
            if old_listener == new_listener:
                return
            await self._close_unlocked()
            try:
                await self._start_unlocked()
            except Exception:
                self.settings = old_settings
                await self._start_unlocked()
                raise

    async def _start_unlocked(self) -> None:
        if self._runner is not None or not self.settings.public_https_base_url:
            return
        app = web.Application(client_max_size=_MAX_JSON_BYTES, middlewares=[self._security_headers])
        app.add_routes(
            [
                web.get(f"{PUBLIC_LOGIN_PREFIX}/login/session", self._session_page),
                web.get(f"{PUBLIC_LOGIN_PREFIX}/login/{{link_token}}", self._login_page),
                web.post(f"{PUBLIC_LOGIN_PREFIX}/login/exchange", self._exchange),
                web.post(f"{PUBLIC_LOGIN_PREFIX}/login/submit", self._submit),
                web.post(f"{PUBLIC_LOGIN_PREFIX}/login/select", self._select),
            ]
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(
                runner,
                self.settings.login_server_host,
                self.settings.login_server_port,
            )
            await site.start()
        except Exception as exc:
            await runner.cleanup()
            raise PublicLoginServerError(
                "无法启动独立登录服务 "
                f"{self.settings.login_server_host}:{self.settings.login_server_port}：{exc}"
            ) from exc
        self._runner = runner

    async def _close_unlocked(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()

    @web.middleware
    async def _security_headers(self, request: web.Request, handler):
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    async def _login_page(self, request: web.Request) -> web.Response:
        service = self._service()
        if not await service.validate_link(request.match_info["link_token"]):
            return self._error("登录链接无效或已过期", 410)
        return web.Response(
            body=_LOGIN_PAGE.read_bytes(), content_type="text/html", charset="utf-8"
        )

    async def _session_page(self, _request: web.Request) -> web.Response:
        if self.login_sessions() is None:
            return self._error("登录服务尚未初始化", 503)
        return web.Response(
            body=_LOGIN_PAGE.read_bytes(), content_type="text/html", charset="utf-8"
        )

    async def _exchange(self, request: web.Request) -> web.Response:
        try:
            payload = await self._payload(request)
            session = await self._service().exchange_link(str(payload.get("link_token") or ""))
            return web.json_response(
                {
                    "session_token": session.session_token,
                    "csrf_token": session.csrf_token,
                    "expires_at": session.expires_at.isoformat(),
                }
            )
        except LoginSessionError as exc:
            return self._login_error(exc)

    async def _submit(self, request: web.Request) -> web.Response:
        try:
            payload = await self._payload(request)
            result = await self._service().submit_credentials(
                str(payload.get("session_token") or ""),
                str(payload.get("csrf_token") or ""),
                str(payload.get("email") or ""),
                str(payload.get("password") or ""),
                self._origin(request),
                self._client_ip(request),
                payload.get("geetest") if isinstance(payload.get("geetest"), dict) else None,
            )
            if result.risk_required:
                return web.json_response({"status": "risk", "captcha_id": result.captcha_id})
            return web.json_response(
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
        except LoginSessionError as exc:
            return self._login_error(exc)

    async def _select(self, request: web.Request) -> web.Response:
        try:
            payload = await self._payload(request)
            selected = payload.get("selected_uids")
            if not isinstance(selected, list):
                raise LoginSessionError("请求格式无效")
            result = await self._service().select_uids(
                str(payload.get("session_token") or ""),
                str(payload.get("csrf_token") or ""),
                self._origin(request),
                [str(uid) for uid in selected],
                str(payload.get("default_uid") or ""),
            )
            return web.json_response(
                {
                    "status": "awaiting_confirm",
                    "confirmation_code": result.confirmation_code,
                    "expires_at": result.expires_at.isoformat(),
                }
            )
        except LoginSessionError as exc:
            return self._login_error(exc)

    async def _payload(self, request: web.Request) -> dict[str, Any]:
        if self._origin(request) != self.settings.public_https_base_url:
            raise PublicLoginRequestError("登录页面来源校验失败", 403)
        if request.content_type.casefold() != "application/json":
            raise PublicLoginRequestError("仅接受 JSON 请求", 415)
        try:
            payload = await request.json()
        except (ValueError, TypeError) as exc:
            raise LoginSessionError("请求格式无效") from exc
        if not isinstance(payload, dict):
            raise LoginSessionError("请求格式无效")
        return payload

    def _client_ip(self, request: web.Request) -> str:
        if self.settings.login_trust_proxy_headers:
            candidates = [
                request.headers.get("CF-Connecting-IP", ""),
                request.headers.get("X-Forwarded-For", "").split(",", 1)[0],
                request.headers.get("X-Real-IP", ""),
            ]
            for candidate in candidates:
                try:
                    return str(ipaddress.ip_address(candidate.strip()))
                except ValueError:
                    continue
        return str(request.remote or "unknown")

    @staticmethod
    def _origin(request: web.Request) -> str:
        return str(request.headers.get("Origin") or "").rstrip("/")

    def _service(self) -> LoginSessionService:
        service = self.login_sessions()
        if service is None:
            raise LoginSessionError("登录服务尚未初始化")
        return service

    @staticmethod
    def _error(message: str, status: int = 400) -> web.Response:
        return web.json_response({"status": "error", "message": message}, status=status)

    @classmethod
    def _login_error(cls, error: LoginSessionError) -> web.Response:
        return cls._error(str(error), getattr(error, "status", 400))

    @staticmethod
    def _listener_key(settings: PluginSettings) -> tuple[bool, str, int]:
        return (
            bool(settings.public_https_base_url),
            settings.login_server_host,
            settings.login_server_port,
        )
