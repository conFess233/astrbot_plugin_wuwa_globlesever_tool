"""独立于 AstrBot Dashboard 鉴权的短期登录 HTTP 服务。"""

import asyncio
import hmac
import ipaddress
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web
from astrbot.api import logger

from ..constants import PUBLIC_LOGIN_PREFIX
from ..domain.login import GuidePlayer, LoginCompletionResult
from ..services.login_sessions import LoginSessionError, LoginSessionService
from ..services.settings import PluginSettings

_LOGIN_PAGE = Path(__file__).with_name("static") / "login.html"
_MAX_JSON_BYTES = 64 * 1024
_SESSION_COOKIE = "wuwa_login_session"
_CSRF_COOKIE = "wuwa_login_csrf"
_CompletionHandler = Callable[[LoginCompletionResult], Awaitable[None]]


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
        on_complete: _CompletionHandler,
    ) -> None:
        self.settings = settings
        self.login_sessions = login_sessions
        self.on_complete = on_complete
        self._runner: web.AppRunner | None = None
        self._lock = asyncio.Lock()
        self._completion_tasks: set[asyncio.Task[None]] = set()
        self._page_bytes: bytes | None = None
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None

    async def start(self) -> None:
        async with self._lock:
            if self._runner is not None or not self.settings.public_https_base_url:
                return
            try:
                self._runner = await self._build_runner(self.settings)
                self.last_error = None
            except PublicLoginServerError as exc:
                self.last_error = str(exc)
                raise
            except Exception as exc:
                self.last_error = str(exc)
                raise PublicLoginServerError("无法初始化独立登录服务") from exc

    async def close(self) -> None:
        async with self._lock:
            runner, self._runner = self._runner, None
            if runner is not None:
                await runner.cleanup()
        tasks = tuple(self._completion_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._completion_tasks.clear()

    async def update_settings(self, settings: PluginSettings) -> None:
        async with self._lock:
            old_key = self._listener_key(self.settings)
            new_key = self._listener_key(settings)
            if old_key == new_key:
                self.settings = settings
                self.last_error = None
                return
            if not settings.public_https_base_url:
                old_runner, self._runner = self._runner, None
                self.settings = settings
                self.last_error = None
                if old_runner is not None:
                    await old_runner.cleanup()
                return
            try:
                new_runner = await self._build_runner(settings)
            except Exception as exc:
                self.last_error = str(exc)
                raise
            old_runner = self._runner
            self._runner = new_runner
            self.settings = settings
            self.last_error = None
            if old_runner is not None:
                await old_runner.cleanup()

    async def _build_runner(self, settings: PluginSettings) -> web.AppRunner:
        app = web.Application(client_max_size=_MAX_JSON_BYTES, middlewares=[self._security_headers])
        app.add_routes(
            [
                web.get(f"{PUBLIC_LOGIN_PREFIX}/health", self._health),
                web.get(f"{PUBLIC_LOGIN_PREFIX}/login", self._serve_login_page),
                web.get(f"{PUBLIC_LOGIN_PREFIX}/login/{{link_token}}", self._login_link),
                web.post(f"{PUBLIC_LOGIN_PREFIX}/api/session/exchange", self._exchange),
                web.post(f"{PUBLIC_LOGIN_PREFIX}/api/session/login", self._login),
                web.post(f"{PUBLIC_LOGIN_PREFIX}/api/session/captcha", self._captcha),
                web.post(f"{PUBLIC_LOGIN_PREFIX}/api/session/accounts", self._accounts),
                web.post(f"{PUBLIC_LOGIN_PREFIX}/api/session/complete", self._complete),
            ]
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, settings.login_server_host, settings.login_server_port)
            await site.start()
        except Exception as exc:
            await runner.cleanup()
            raise PublicLoginServerError(
                f"无法启动独立登录服务 {settings.login_server_host}:{settings.login_server_port}"
            ) from exc
        return runner

    @web.middleware
    async def _security_headers(self, request: web.Request, handler) -> web.StreamResponse:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = self._error(exc.reason or "请求无效", exc.status)
        except Exception:
            logger.exception("鸣潮国际服登录服务请求处理失败")
            response = self._error("登录服务暂时不可用", 500)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' "
            "https://static.geetest.com https://*.geetest.com; connect-src 'self' "
            "https://*.geetest.com; frame-src https://*.geetest.com; img-src 'self' data: "
            "https://*.geetest.com"
        )
        return response

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok" if self.login_sessions() is not None else "starting",
                "service": "wuwa-global-login",
            }
        )

    async def _serve_login_page(self, _request: web.Request) -> web.Response:
        if self._page_bytes is None:
            self._page_bytes = await asyncio.to_thread(_LOGIN_PAGE.read_bytes)
        return web.Response(body=self._page_bytes, content_type="text/html", charset="utf-8")

    async def _login_link(self, request: web.Request) -> web.StreamResponse:
        try:
            session = await self._service().exchange_link(
                str(request.match_info["link_token"]),
                self._client_ip(request),
            )
        except LoginSessionError as exc:
            return self._login_error(exc, status=410)
        response = web.Response(
            status=303,
            headers={"Location": f"{PUBLIC_LOGIN_PREFIX}/login"},
        )
        self._set_session_cookies(
            response, session.session_token, session.csrf_token, session.expires_at
        )
        return response

    async def _exchange(self, request: web.Request) -> web.Response:
        try:
            payload = await self._payload(request, require_csrf=False)
            session = await self._service().exchange_link(
                str(payload.get("link_token") or ""),
                self._client_ip(request),
            )
            response = web.json_response(
                {"status": "active", "redirect_url": f"{PUBLIC_LOGIN_PREFIX}/login"}
            )
            self._set_session_cookies(
                response,
                session.session_token,
                session.csrf_token,
                session.expires_at,
            )
            return response
        except LoginSessionError as exc:
            return self._login_error(exc)

    async def _accounts(self, request: web.Request) -> web.Response:
        try:
            await self._payload(request)
            session_token, csrf_token = self._session_credentials(request)
            state = await self._service().browser_state(
                session_token,
                csrf_token,
                self._origin(request),
            )
            return web.json_response(
                {
                    "status": state.status,
                    "expires_at": state.expires_at.isoformat(),
                    "email_masked": state.email_masked,
                    "players": [self._player_payload(player) for player in state.players],
                }
            )
        except LoginSessionError as exc:
            return self._login_error(exc)

    async def _login(self, request: web.Request) -> web.Response:
        return await self._submit_credentials(request, endpoint="login")

    async def _captcha(self, request: web.Request) -> web.Response:
        return await self._submit_credentials(request, endpoint="captcha")

    async def _submit_credentials(self, request: web.Request, *, endpoint: str) -> web.Response:
        try:
            payload = await self._payload(request)
            session_token, csrf_token = self._session_credentials(request)
            result = await self._service().submit_credentials(
                session_token,
                csrf_token,
                str(payload.get("email") or ""),
                str(payload.get("password") or ""),
                self._origin(request),
                self._client_ip(request),
                payload.get("geetest") if isinstance(payload.get("geetest"), dict) else None,
                endpoint=endpoint,
            )
            if result.risk_required:
                return web.json_response({"status": "risk", "captcha_id": result.captcha_id})
            return web.json_response(
                {
                    "status": "selecting",
                    "email_masked": result.email_masked,
                    "players": [self._player_payload(player) for player in result.players],
                }
            )
        except LoginSessionError as exc:
            return self._login_error(exc)

    async def _complete(self, request: web.Request) -> web.Response:
        try:
            payload = await self._payload(request)
            selected = payload.get("selected_accounts")
            default = payload.get("default_account")
            if (
                not isinstance(selected, list)
                or any(not isinstance(item, dict) for item in selected)
                or not isinstance(default, dict)
            ):
                raise LoginSessionError("请求格式无效")
            session_token, csrf_token = self._session_credentials(request)
            result = await self._service().complete_accounts(
                session_token,
                csrf_token,
                self._origin(request),
                selected,
                default,
            )
            self._schedule_completion(result)
            response = web.json_response(
                {
                    "status": "completed",
                    "email_masked": result.email_masked,
                    "selected_count": len(result.selected_accounts),
                    "default_account": {
                        "region_id": result.default_account.region_id,
                        "uid": result.default_account.uid,
                    },
                }
            )
            self._clear_session_cookies(response)
            return response
        except LoginSessionError as exc:
            return self._login_error(exc)

    async def _payload(
        self,
        request: web.Request,
        *,
        require_csrf: bool = True,
    ) -> dict[str, Any]:
        if self._origin(request) != self.settings.public_https_base_url:
            raise PublicLoginRequestError("登录页面来源校验失败", 403)
        if request.content_type.casefold() != "application/json":
            raise PublicLoginRequestError("仅接受 JSON 请求", 415)
        if require_csrf:
            self._session_credentials(request)
        try:
            payload = await request.json()
        except (ValueError, TypeError) as exc:
            raise LoginSessionError("请求格式无效") from exc
        if not isinstance(payload, dict):
            raise LoginSessionError("请求格式无效")
        return payload

    def _session_credentials(self, request: web.Request) -> tuple[str, str]:
        session_token = str(request.cookies.get(_SESSION_COOKIE) or "")
        csrf_cookie = str(request.cookies.get(_CSRF_COOKIE) or "")
        csrf_header = str(request.headers.get("X-CSRF-Token") or "")
        if (
            not session_token
            or not csrf_cookie
            or len(csrf_cookie) > 256
            or len(csrf_header) > 256
            or not hmac.compare_digest(csrf_cookie, csrf_header)
        ):
            raise LoginSessionError("登录会话无效或已过期")
        return session_token, csrf_cookie

    def _client_ip(self, request: web.Request) -> str:
        remote = str(request.remote or "").strip()
        try:
            peer = ipaddress.ip_address(remote)
        except ValueError:
            return "unknown"
        trusted_networks = tuple(
            ipaddress.ip_network(value, strict=False)
            for value in self.settings.login_trusted_proxy_cidrs
        )
        wildcard_listener = self.settings.login_server_host in {"0.0.0.0", "::"}
        trusted_peer = any(peer in network for network in trusted_networks) or (
            peer.is_loopback and not wildcard_listener
        )
        if self.settings.login_trust_proxy_headers and trusted_peer:
            candidates = (
                request.headers.get("CF-Connecting-IP", ""),
                request.headers.get("X-Forwarded-For", "").split(",", 1)[0],
                request.headers.get("X-Real-IP", ""),
            )
            for candidate in candidates:
                try:
                    return str(ipaddress.ip_address(candidate.strip()))
                except ValueError:
                    continue
        return str(peer)

    def _schedule_completion(self, result: LoginCompletionResult) -> None:
        task = asyncio.create_task(self.on_complete(result))
        self._completion_tasks.add(task)
        task.add_done_callback(self._completion_done)

    def _completion_done(self, task: asyncio.Task[None]) -> None:
        self._completion_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("鸣潮国际服登录完成通知失败")

    @staticmethod
    def _set_session_cookies(
        response: web.StreamResponse,
        session_token: str,
        csrf_token: str,
        expires_at: datetime,
    ) -> None:
        max_age = max(1, int((expires_at - datetime.now(UTC)).total_seconds()))
        response.set_cookie(
            _SESSION_COOKIE,
            session_token,
            max_age=max_age,
            secure=True,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        response.set_cookie(
            _CSRF_COOKIE,
            csrf_token,
            max_age=max_age,
            secure=True,
            httponly=False,
            samesite="Strict",
            path="/",
        )

    @staticmethod
    def _clear_session_cookies(response: web.StreamResponse) -> None:
        response.del_cookie(_SESSION_COOKIE, path="/")
        response.del_cookie(_CSRF_COOKIE, path="/")

    def _service(self) -> LoginSessionService:
        service = self.login_sessions()
        if service is None:
            raise LoginSessionError("登录服务尚未初始化")
        return service

    @staticmethod
    def _origin(request: web.Request) -> str:
        return str(request.headers.get("Origin") or "").rstrip("/")

    @staticmethod
    def _player_payload(player: GuidePlayer) -> dict[str, object]:
        return {
            "uid": player.uid,
            "player_name": player.player_name,
            "region_id": player.region_id,
            "region_name": player.region_name,
            "level": player.level,
        }

    @staticmethod
    def _error(message: str, status: int = 400) -> web.Response:
        return web.json_response({"status": "error", "message": message}, status=status)

    @classmethod
    def _login_error(
        cls,
        error: LoginSessionError,
        *,
        status: int | None = None,
    ) -> web.Response:
        return cls._error(str(error), status or getattr(error, "status", 400))

    @staticmethod
    def _listener_key(settings: PluginSettings) -> tuple[bool, str, int]:
        return (
            bool(settings.public_https_base_url),
            settings.login_server_host,
            settings.login_server_port,
        )
