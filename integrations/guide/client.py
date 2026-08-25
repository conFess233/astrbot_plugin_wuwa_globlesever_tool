"""攻略站玩家选择、角色列表与攻略详情客户端。"""

import asyncio
import json
from typing import Any
from urllib.parse import urlencode

import aiohttp

from ...domain.sync import (
    GuideAuthenticationError,
    GuideAvatar,
    GuideError,
    GuideIntroduction,
    GuideRoleDetail,
    GuideSyncPlayer,
    GuideUnavailableError,
)
from ...infrastructure.network import HttpClient

_BASES = ("https://guide-server.aki-game.net", "https://guide-server-1.aki-game.net")
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class GlobalGuideClient:
    def __init__(self, http: HttpClient):
        self.http = http

    async def login(self, c_uid: str, c_name: str, access_token: str, language: str) -> str:
        payload = await self._request(
            "POST",
            "/user/login/sdk",
            language,
            body={"cUid": c_uid, "cName": c_name, "accessToken": access_token},
        )
        data = payload.get("data")
        token = str(data.get("token") or "").strip() if isinstance(data, dict) else ""
        if not token:
            raise GuideAuthenticationError("攻略站登录状态无效，请重新登录")
        return token

    async def players(self, token: str, language: str) -> tuple[GuideSyncPlayer, ...]:
        payload = await self._request("GET", "/user/player/list", language, token=token)
        data = payload.get("data")
        if not isinstance(data, list):
            raise GuideError("攻略站玩家列表格式无效")
        result = []
        for item in data:
            if not isinstance(item, dict):
                continue
            uid = str(item.get("playerId") or "").strip()
            region_id = str(item.get("serverId") or "").strip()
            if uid.isdigit() and region_id:
                result.append(
                    GuideSyncPlayer(
                        uid,
                        region_id,
                        str(item.get("serverName") or region_id),
                        str(item.get("playerName")) if item.get("playerName") else None,
                    )
                )
        return tuple(result)

    async def choose_player(self, token: str, language: str, uid: str, region_id: str) -> None:
        await self._request(
            "POST",
            "/user/player/choose",
            language,
            token=token,
            body={"playerId": int(uid), "serverId": region_id},
        )

    async def avatars(self, token: str, language: str) -> tuple[GuideAvatar, ...]:
        payload = await self._request("GET", "/role/avatar/list", language, token=token)
        data = payload.get("data")
        if not isinstance(data, list):
            raise GuideError("攻略站角色列表格式无效")
        result = []
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            role_id = str(item.get("roleGbId") or "").strip()
            if role_id:
                result.append(GuideAvatar(role_id, item.get("isAcquired") is True, index))
        if not result:
            raise GuideError("攻略站角色列表为空或格式无效")
        return tuple(result)

    async def introductions(
        self, token: str, language: str, role_id: str
    ) -> tuple[GuideIntroduction, ...]:
        query = urlencode({"roleGbId": role_id})
        payload = await self._request("GET", f"/introduction/list?{query}", language, token=token)
        data = payload.get("data")
        if not isinstance(data, list):
            raise GuideError("攻略站角色攻略列表格式无效")
        result = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                introduction_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            texts = item.get("texts") if isinstance(item.get("texts"), list) else []
            languages = tuple(
                str(text.get("language"))
                for text in texts
                if isinstance(text, dict) and text.get("language")
            )
            modified = item.get("modifiedAt")
            try:
                modified_at = int(modified) if modified is not None else None
            except (TypeError, ValueError):
                modified_at = None
            result.append(GuideIntroduction(introduction_id, languages, modified_at))
        return tuple(result)

    async def introduction_detail(
        self, token: str, language: str, role_id: str, introduction_id: int
    ) -> GuideRoleDetail | None:
        query = urlencode({"roleGbId": role_id, "id": introduction_id})
        payload = await self._request("GET", f"/introduction/info?{query}", language, token=token)
        data = payload.get("data")
        if data is None:
            return None
        if not isinstance(data, dict):
            raise GuideError("攻略站角色攻略详情格式无效")
        chain = None
        resonance = data.get("roleResonance")
        if isinstance(resonance, dict) and isinstance(resonance.get("items"), list):
            chain = sum(
                1
                for item in resonance["items"]
                if isinstance(item, dict) and item.get("isAcquired") is True
            )
        weapon_present = None
        weapon_id = None
        weapon_name = None
        weapon_picture_url = None
        weapon_star = None
        weapon_type_id = None
        weapon_type_picture_url = None
        weapon = data.get("weapon")
        if isinstance(weapon, dict) and "current" in weapon:
            current = weapon.get("current")
            if isinstance(current, dict) and str(current.get("gbId") or "").strip():
                weapon_present = True
                weapon_id = str(current["gbId"]).strip()
                weapon_name = self._localized_name(current, language)
                weapon_picture_url = self._optional_url(current.get("pictureUrl"))
                weapon_star = self._integer(current.get("star"))
                weapon_type = current.get("weaponType")
                if isinstance(weapon_type, dict):
                    weapon_type_id = str(weapon_type.get("gbId") or "").strip() or None
                    weapon_type_picture_url = self._optional_url(weapon_type.get("pictureUrl"))
            else:
                weapon_present = False
        return GuideRoleDetail(
            chain,
            weapon_present,
            weapon_id,
            weapon_name,
            weapon_picture_url,
            weapon_star,
            weapon_type_id,
            weapon_type_picture_url,
        )

    async def _request(
        self,
        method: str,
        path: str,
        language: str,
        *,
        token: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self.http.session
        if session is None or session.closed:
            raise GuideUnavailableError("HTTP 客户端尚未初始化")
        headers = {"x-language": language, "Accept-Language": language}
        if token:
            headers["x-token"] = token
        last_error: Exception | None = None
        for index, base in enumerate(_BASES):
            try:
                async with session.request(
                    method, f"{base}{path}", headers=headers, json=body
                ) as response:
                    if response.status in {401, 403}:
                        raise GuideAuthenticationError("攻略站登录状态失效，请重新登录")
                    if response.status >= 500 and index + 1 < len(_BASES):
                        continue
                    if response.status != 200:
                        raise GuideUnavailableError("攻略站请求失败，请稍后重试")
                    content = await response.read()
                    if len(content) > _MAX_RESPONSE_BYTES:
                        raise GuideError("攻略站响应超过安全大小限制")
                    try:
                        payload = json.loads(content)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise GuideError("攻略站响应格式无效") from exc
                    if not isinstance(payload, dict):
                        raise GuideError("攻略站响应格式无效")
                    code = self._integer(payload.get("code"))
                    if code in {401, 403, 1000}:
                        raise GuideAuthenticationError("攻略站登录状态失效，请重新登录")
                    if code != 200:
                        raise GuideError("攻略站拒绝了同步请求")
                    return payload
            except GuideAuthenticationError:
                raise
            except GuideError:
                raise
            except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
                last_error = exc
                if index + 1 >= len(_BASES):
                    break
        raise GuideUnavailableError("无法连接攻略站，请稍后重试") from last_error

    @staticmethod
    def _integer(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_url(value: object) -> str | None:
        result = str(value or "").strip()
        return result if result.startswith("https://guide-res.aki-game.net/") else None

    @staticmethod
    def _localized_name(value: dict[str, Any], language: str) -> str | None:
        direct = str(value.get("name") or "").strip()
        if direct:
            return direct
        texts = value.get("texts")
        if not isinstance(texts, list):
            return None
        candidates = [item for item in texts if isinstance(item, dict)]
        preferred = next(
            (
                item
                for item in candidates
                if str(item.get("language") or "").casefold() == language.casefold()
            ),
            None,
        )
        selected = preferred or (candidates[0] if candidates else None)
        result = str((selected or {}).get("name") or "").strip()
        return result or None
