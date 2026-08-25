"""国际服 SDK 登录、OAuth 与攻略站账号发现客户端。"""

import asyncio
import json
import uuid
from typing import Any

import aiohttp

from ...domain.login import (
    AuthenticatedAccount,
    AuthenticationError,
    AuthenticationUnavailableError,
    GeetestChallenge,
    GuidePlayer,
    SdkLoginResult,
)
from ...infrastructure.network import HttpClient
from .sdk_crypto import SdkEncodingError, encode_password, generate_signature

_SDK_BASE = "https://sdkapi.kurogame-service.com"
_GUIDE_BASES = (
    "https://guide-server.aki-game.net",
    "https://guide-server-1.aki-game.net",
)
_EMAIL_LOGIN_PATH = "/sdkcom/v2/login/emailPwd.lg"
_GET_TOKEN_PATH = "/sdkcom/v2/auth/getToken.lg"
_GENERATE_PATH = "/sdkcom/v2/user/oauth/code/generate.lg"
_CLIENT_ID = "7rxmydkibzzsf12om5asjnoo"
_CLIENT_SECRET = "32gh5r0p35ullmxrzzwk40ly"
_PRODUCT_KEY = "5c063821193f41e09f1c4fdd7567dda3"
_PROJECT_ID = "G153"
_SDK_VERSION = "2.6.0h"
_MAX_RESPONSE_BYTES = 1024 * 1024


class GlobalAuthClient:
    def __init__(self, http: HttpClient):
        self.http = http

    @staticmethod
    def new_device_id() -> str:
        return str(uuid.uuid4()).upper()

    async def email_login(
        self,
        email: str,
        password: str,
        device_id: str,
        geetest: dict[str, str] | None = None,
    ) -> SdkLoginResult:
        fields = {
            "__e__": "1",
            "email": email,
            "client_id": _CLIENT_ID,
            "deviceNum": device_id,
            "password": self._encoded_password(password),
            "platform": "PC",
            "productId": "A1730",
            "productKey": _PRODUCT_KEY,
            "projectId": _PROJECT_ID,
            "redirect_uri": "1",
            "response_type": "code",
            "sdkVersion": _SDK_VERSION,
            "channelId": "240",
        }
        if geetest:
            mapping = {
                "captcha_output": "geetestCaptchaOutput",
                "gen_time": "geetestGenTime",
                "lot_number": "geetestLotNumber",
                "pass_token": "geetestPassToken",
            }
            for source, target in mapping.items():
                value = str(geetest.get(source, "")).strip()
                if value:
                    fields[target] = value
        fields["sign"] = generate_signature(fields)
        payload = await self._sdk_form(_EMAIL_LOGIN_PATH, fields)
        codes = self._integer(payload.get("codes"), default=-1)
        if codes == 41000:
            return SdkLoginResult(True, challenge=GeetestChallenge())
        if codes != 0:
            raise AuthenticationError("邮箱或密码错误，或账号暂时无法登录")
        code = self._required_text(payload, "code", "登录响应缺少授权码")
        c_uid = self._required_text(payload, "cuid", "登录响应缺少账号标识")
        c_name = str(payload.get("username") or c_uid)
        return SdkLoginResult(
            False,
            code=code,
            c_uid=c_uid,
            c_name=c_name,
            auto_token=self._optional_text(payload.get("autoToken")),
        )

    async def complete_login(
        self,
        result: SdkLoginResult,
        device_id: str,
        language: str = "zh-Hans",
    ) -> AuthenticatedAccount:
        if result.risk_required or not result.code or not result.c_uid or not result.c_name:
            raise AuthenticationError("登录上下文不完整")
        access_token = await self._get_access_token(result.code, device_id)
        oauth_code = await self._generate_oauth(access_token, device_id)
        guide_token = await self._guide_login(
            result.c_uid,
            result.c_name,
            access_token,
            language,
        )
        players = await self._guide_players(guide_token, language)
        if not players:
            raise AuthenticationError("该账号没有可绑定的国际服 UID")
        return AuthenticatedAccount(
            c_uid=result.c_uid,
            c_name=result.c_name,
            auto_token=result.auto_token,
            access_token=access_token,
            oauth_code=oauth_code,
            guide_token=guide_token,
            device_id=device_id,
            players=players,
        )

    async def _get_access_token(self, code: str, device_id: str) -> str:
        fields = {
            "client_id": _CLIENT_ID,
            "deviceNum": device_id,
            "productId": "A1725",
            "projectId": _PROJECT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "client_secret": _CLIENT_SECRET,
            "redirect_uri": "1",
        }
        fields["sign"] = generate_signature(fields)
        payload = await self._sdk_form(_GET_TOKEN_PATH, fields)
        if self._integer(payload.get("codes"), default=-1) != 0:
            raise AuthenticationError("无法获取国际服访问令牌")
        return self._required_text(payload, "access_token", "访问令牌响应不完整")

    async def _generate_oauth(self, access_token: str, device_id: str) -> str:
        fields = {
            "client_id": _CLIENT_ID,
            "deviceNum": device_id,
            "client_secret": _CLIENT_SECRET,
            "access_token": access_token,
            "productId": "A1725",
            "projectId": _PROJECT_ID,
            "redirect_uri": "1",
            "scope": "launcher",
        }
        payload = await self._sdk_form(_GENERATE_PATH, fields)
        if self._integer(payload.get("codes"), default=-1) != 0:
            raise AuthenticationError("无法生成国际服授权码")
        return self._required_text(payload, "oauthCode", "授权码响应不完整")

    async def _guide_login(
        self,
        c_uid: str,
        c_name: str,
        access_token: str,
        language: str,
    ) -> str:
        payload = await self._guide_request(
            "POST",
            "/user/login/sdk",
            language,
            json_body={"cUid": c_uid, "cName": c_name, "accessToken": access_token},
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise AuthenticationError("攻略站登录响应不完整")
        return self._required_text(data, "token", "攻略站登录未返回令牌")

    async def _guide_players(self, token: str, language: str) -> tuple[GuidePlayer, ...]:
        payload = await self._guide_request(
            "GET",
            "/user/player/list",
            language,
            token=token,
        )
        raw_players = payload.get("data")
        if not isinstance(raw_players, list):
            raise AuthenticationError("攻略站玩家列表响应不完整")
        result: list[GuidePlayer] = []
        for raw in raw_players:
            if not isinstance(raw, dict):
                continue
            uid = str(raw.get("playerId") or "").strip()
            region_id = str(raw.get("serverId") or "").strip()
            if not uid or not uid.isdigit() or not region_id:
                continue
            result.append(
                GuidePlayer(
                    uid=uid,
                    player_name=self._optional_text(raw.get("playerName")),
                    region_id=region_id,
                    region_name=str(raw.get("serverName") or region_id),
                    level=self._integer(raw.get("level"), default=None),
                )
            )
        return tuple(result)

    async def _sdk_form(self, path: str, fields: dict[str, str]) -> dict[str, Any]:
        session = self._session()
        try:
            async with session.post(
                f"{_SDK_BASE}{path}",
                data=fields,
                allow_redirects=False,
            ) as response:
                if response.status >= 500:
                    raise AuthenticationUnavailableError("国际服登录服务暂时不可用")
                if response.status != 200:
                    raise AuthenticationError("国际服登录请求被拒绝")
                return await self._read_json(response)
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
            raise AuthenticationUnavailableError("无法连接国际服登录服务") from exc

    async def _guide_request(
        self,
        method: str,
        path: str,
        language: str,
        *,
        token: str | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._session()
        headers = {"x-language": language, "Accept-Language": language}
        if token:
            headers["x-token"] = token
        last_error: Exception | None = None
        for index, base in enumerate(_GUIDE_BASES):
            try:
                async with session.request(
                    method,
                    f"{base}{path}",
                    headers=headers,
                    json=json_body,
                    allow_redirects=False,
                ) as response:
                    if response.status in {401, 403}:
                        raise AuthenticationError("攻略站登录状态无效")
                    if response.status >= 500 and index + 1 < len(_GUIDE_BASES):
                        continue
                    if response.status != 200:
                        raise AuthenticationUnavailableError("攻略站服务暂时不可用")
                    payload = await self._read_json(response)
                    code = self._integer(payload.get("code"), default=-1)
                    if code in {401, 403, 1000}:
                        raise AuthenticationError("攻略站登录状态无效")
                    if code != 200:
                        raise AuthenticationError("攻略站拒绝了账号请求")
                    return payload
            except AuthenticationError:
                raise
            except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
                last_error = exc
                if index + 1 >= len(_GUIDE_BASES):
                    break
        raise AuthenticationUnavailableError("无法连接攻略站服务") from last_error

    def _session(self) -> aiohttp.ClientSession:
        session = self.http.session
        if session is None or session.closed:
            raise AuthenticationUnavailableError("HTTP 客户端尚未初始化")
        return session

    @staticmethod
    async def _read_json(response: aiohttp.ClientResponse) -> dict[str, Any]:
        content = await response.read()
        if len(content) > _MAX_RESPONSE_BYTES:
            raise AuthenticationUnavailableError("上游响应过大")
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthenticationUnavailableError("上游响应格式无效") from exc
        if not isinstance(payload, dict):
            raise AuthenticationUnavailableError("上游响应格式无效")
        return payload

    @staticmethod
    def _required_text(payload: dict[str, Any], key: str, message: str) -> str:
        value = str(payload.get(key) or "").strip()
        if not value:
            raise AuthenticationError(message)
        return value

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        result = str(value or "").strip()
        return result or None

    @staticmethod
    def _integer(value: Any, *, default: int | None) -> int | None:
        if isinstance(value, bool):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _encoded_password(password: str) -> str:
        try:
            return encode_password(password)
        except SdkEncodingError as exc:
            raise AuthenticationError(str(exc)) from exc
