"""从国际服启动器接口刷新并持久化唯一玩家详情快照。"""

import asyncio
import hashlib
import json
import random
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from ...domain.models import RegionUid
from ...domain.player import PlayerDataError, PlayerSnapshot
from ...infrastructure.database import Database
from ...infrastructure.network import HttpClient
from ...infrastructure.security import CryptoError, TokenCipher
from ...integrations._guide_http import kuro_headers
from ...integrations.regions import regions_equivalent
from ..settings import PluginSettings
from .coordinator import SingleFlightCoordinator
from .credentials import (
    CredentialRefreshAuthenticationError,
    CredentialRefreshService,
    CredentialRefreshUnavailableError,
)

_PLAYER_INFO_URL = "https://pc-launcher-sdk-api.kurogame.net/game/queryPlayerInfo"
_ROLE_URL = "https://pc-launcher-sdk-api.kurogame.net/game/queryRole"
_ALLOWED_HOSTS = {"pc-launcher-sdk-api.kurogame.net"}
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class _PlayerAuthenticationError(PlayerDataError):
    """表示游戏接口明确拒绝当前 OAuthCode。"""


class _PlayerSessionRejected(PlayerDataError):
    """表示游戏接口拒绝当前会话，可尝试使用 autoToken 续期。"""


@dataclass(frozen=True, slots=True)
class _Context:
    qq_id: str
    uid: str
    region_id: str
    region_name: str
    credential_id: int
    encrypted_tokens: str


class PlayerDataService:
    def __init__(
        self,
        database: Database,
        cipher: TokenCipher,
        http: HttpClient,
        settings: PluginSettings,
        raw_snapshot_directory: Path | None = None,
        credential_refresher: CredentialRefreshService | None = None,
    ):
        self.database = database
        self.cipher = cipher
        self.http = http
        self.settings = settings
        self.raw_snapshot_directory = raw_snapshot_directory
        self.credential_refresher = credential_refresher
        self._singleflight = SingleFlightCoordinator[PlayerSnapshot]()

    async def close(self) -> None:
        await self._singleflight.wait()

    async def query(
        self,
        qq_id: str,
        *,
        external_query: bool = False,
        uid: str | None = None,
        region_id: str | None = None,
    ) -> PlayerSnapshot:
        context = await self._context(
            qq_id,
            external_query=external_query,
            uid=uid,
            region_id=region_id,
        )
        cached = await self._cached_context(context)
        if external_query:
            if cached is None:
                raise PlayerDataError("该用户尚无玩家数据缓存，请让对方先自行查询一次")
            return cached
        if cached is not None and not self.settings.query_refresh_enabled:
            return cached
        if cached is not None and self._inside_cooldown(cached.refreshed_at):
            return cached
        return await self._refresh_with_fallback(context, cached)

    async def refresh(
        self,
        qq_id: str,
        *,
        uid: str | None = None,
        region_id: str | None = None,
    ) -> PlayerSnapshot:
        context = await self._context(
            qq_id,
            external_query=False,
            uid=uid,
            region_id=region_id,
        )
        cached = await self._cached_context(context)
        return await self._refresh_with_fallback(context, cached)

    async def _refresh_with_fallback(
        self,
        context: _Context,
        cached: PlayerSnapshot | None,
    ) -> PlayerSnapshot:
        flight_key = RegionUid(context.region_id, context.uid).cache_key
        try:
            return await self._singleflight.run(flight_key, lambda: self._refresh(context))
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
            CryptoError,
            PlayerDataError,
            json.JSONDecodeError,
        ) as exc:
            if cached is not None:
                return replace(cached, is_cached_fallback=True)
            raise PlayerDataError("玩家详情刷新失败，且本地没有可用缓存，请重新登录后重试") from exc

    async def cached(
        self,
        qq_id: str,
        *,
        external_query: bool = False,
        uid: str | None = None,
        region_id: str | None = None,
    ) -> PlayerSnapshot | None:
        context = await self._context(
            qq_id,
            external_query=external_query,
            uid=uid,
            region_id=region_id,
        )
        return await self._cached_context(context)

    async def _cached_context(self, context: _Context) -> PlayerSnapshot | None:
        return await self.database.read(
            lambda db: self._snapshot_from_row(
                db.execute(
                    "SELECT s.*, g.region_id, g.region_name FROM player_snapshots s "
                    "JOIN game_accounts g ON g.region_id = s.region_id AND g.uid = s.uid "
                    "WHERE s.region_id = ? AND s.uid = ? AND g.qq_id = ?",
                    (context.region_id, context.uid, context.qq_id),
                ).fetchone()
            )
        )

    async def _refresh(self, context: _Context) -> PlayerSnapshot:
        await self._mark_refresh(context, "attempt")
        try:
            try:
                sensitive = json.loads(self.cipher.decrypt_text(context.encrypted_tokens))
            except (CryptoError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise _PlayerAuthenticationError("本地登录凭据无法读取") from exc
            if not isinstance(sensitive, dict):
                raise _PlayerAuthenticationError("本地登录凭据格式无效")
            oauth_code = str(sensitive.get("oauth_code") or "").strip()
            if not oauth_code:
                oauth_code = await self._renew_session(context)
            async with asyncio.timeout(self.settings.player_refresh_timeout_seconds):
                try:
                    player_response, role_response, player, detail = await self._fetch_upstream(
                        context,
                        oauth_code,
                    )
                except _PlayerSessionRejected:
                    if self.credential_refresher is None:
                        raise
                    oauth_code = await self._renew_session(context)
                    player_response, role_response, player, detail = await self._fetch_upstream(
                        context,
                        oauth_code,
                    )
        except _PlayerAuthenticationError:
            await self._mark_refresh(context, "failure")
            await self._mark_game_auth_invalid(context)
            raise
        except Exception:
            await self._mark_refresh(context, "failure")
            raise
        try:
            base = detail.get("Base")
            if not isinstance(base, dict):
                raise ValueError("玩家详情响应缺少 Base")
            base_uid = self._integer(base.get("Id"))
            if base_uid is not None and str(base_uid) != context.uid:
                raise ValueError("玩家详情响应中的 UID 与目标账号不一致")
            battle_pass = detail.get("BattlePass")
            battle_pass = battle_pass if isinstance(battle_pass, dict) else None
        except Exception:
            await self._mark_refresh(context, "failure")
            raise
        refreshed_at = datetime.now(UTC).isoformat()
        level = self._integer(base.get("Level"))
        if level is None:
            level = self._integer(player.get("level"))
        values = {
            "player_name": self._text(player.get("roleName")) or self._text(base.get("Name")),
            "head_photo": self._integer(player.get("headPhoto")),
            "level": level,
            "world_level": self._integer(base.get("WorldLevel")),
            "role_num": self._integer(base.get("RoleNum")),
            "active_days": self._integer(base.get("ActiveDays")),
            "created_at_ms": self._integer(base.get("CreatTime")),
            "energy": self._integer(base.get("Energy")),
            "max_energy": self._integer(base.get("MaxEnergy")),
            "store_energy": self._integer(base.get("StoreEnergy")),
            "max_store_energy": self._integer(base.get("MaxStoreEnergy")),
            "energy_recover_time_ms": self._integer(base.get("EnergyRecoverTime")),
            "store_energy_recover_time_ms": self._integer(base.get("StoreEnergyRecoverTime")),
            "liveness": self._integer(base.get("Liveness")),
            "liveness_max": self._integer(base.get("LivenessMaxCount")),
            "liveness_unlock": self._boolean(base.get("LivenessUnlock")),
            "weekly_inst_count": self._integer(base.get("WeeklyInstCount")),
            "battle_pass_present": int(battle_pass is not None),
            "battle_pass_level": self._integer(battle_pass.get("Level")) if battle_pass else None,
            "battle_pass_week_exp": self._integer(battle_pass.get("WeekExp"))
            if battle_pass
            else None,
            "battle_pass_week_max_exp": self._integer(battle_pass.get("WeekMaxExp"))
            if battle_pass
            else None,
            "battle_pass_is_unlock": self._boolean_integer(battle_pass.get("IsUnlock"))
            if battle_pass
            else None,
            "battle_pass_is_open": self._boolean_integer(battle_pass.get("IsOpen"))
            if battle_pass
            else None,
            "battle_pass_exp": self._integer(battle_pass.get("Exp")) if battle_pass else None,
            "battle_pass_exp_limit": self._integer(battle_pass.get("ExpLimit"))
            if battle_pass
            else None,
            "sound_box": self._integer(base.get("SoundBox")),
            "boxes_json": self._collection_json(base.get("Boxes")),
            "basic_boxes_json": self._collection_json(base.get("BasicBoxes")),
            "phantom_boxes_json": self._collection_json(base.get("PhantomBoxes")),
        }

        def operation(db: sqlite3.Connection) -> PlayerSnapshot:
            columns = tuple(values)
            db.execute(
                f"INSERT INTO player_snapshots "
                f"(region_id, uid, {', '.join(columns)}, refreshed_at) "
                f"VALUES (?, ?, {', '.join('?' for _ in columns)}, ?) "
                "ON CONFLICT(region_id, uid) DO UPDATE SET "
                + ", ".join(f"{column} = excluded.{column}" for column in columns)
                + ", refreshed_at = excluded.refreshed_at",
                (
                    context.region_id,
                    context.uid,
                    *(values[column] for column in columns),
                    refreshed_at,
                ),
            )
            db.execute(
                "UPDATE game_accounts SET player_name = COALESCE(?, player_name) "
                "WHERE region_id = ? AND uid = ? AND qq_id = ?",
                (
                    values["player_name"],
                    context.region_id,
                    context.uid,
                    context.qq_id,
                ),
            )
            row = db.execute(
                "SELECT s.*, g.region_id, g.region_name FROM player_snapshots s "
                "JOIN game_accounts g ON g.region_id = s.region_id AND g.uid = s.uid "
                "WHERE s.region_id = ? AND s.uid = ?",
                (context.region_id, context.uid),
            ).fetchone()
            snapshot = self._snapshot_from_row(row)
            if snapshot is None:
                raise PlayerDataError("玩家详情快照写入失败")
            return snapshot

        snapshot = await self.database.write(operation)
        await self._mark_refresh(context, "success")
        if self.raw_snapshot_directory is not None:
            await asyncio.to_thread(
                self._write_raw_snapshot,
                context,
                player_response,
                role_response,
            )
        return snapshot

    async def _fetch_upstream(
        self,
        context: _Context,
        oauth_code: str,
    ) -> tuple[object, object, dict[str, object], dict[str, object]]:
        player_response = await self._retry_post(
            _PLAYER_INFO_URL,
            {"oauthCode": oauth_code},
        )
        region_id, player = self._select_player(
            player_response,
            context.uid,
            context.region_id,
        )
        role_response = await self._retry_post(
            _ROLE_URL,
            {
                "oauthCode": oauth_code,
                "playerId": int(context.uid),
                "region": region_id,
            },
        )
        detail = self._select_role_detail(role_response, region_id)
        return player_response, role_response, player, detail

    async def _renew_session(self, context: _Context) -> str:
        if self.credential_refresher is None:
            raise _PlayerAuthenticationError("游戏接口授权已失效，请重新执行 /kh 登录")
        try:
            refreshed = await self.credential_refresher.refresh(context.credential_id)
        except CredentialRefreshUnavailableError as exc:
            raise PlayerDataError("国际服登录续期服务暂时不可用") from exc
        except CredentialRefreshAuthenticationError as exc:
            raise _PlayerAuthenticationError("游戏接口授权已失效，请重新执行 /kh 登录") from exc
        return refreshed.oauth_code

    async def _retry_post(self, url: str, body: dict[str, object]) -> object:
        attempts = self.settings.request_retry_count + 1
        for attempt in range(attempts):
            try:
                payload = await self.http.post_json(
                    url,
                    body,
                    allowed_hosts=_ALLOWED_HOSTS,
                    max_bytes=_MAX_RESPONSE_BYTES,
                    headers=kuro_headers(),
                )
                message = str(payload.get("message") or "") if isinstance(payload, dict) else ""
                if "retrying" not in message.casefold() or attempt + 1 >= attempts:
                    return payload
                await asyncio.sleep((0.35 * (2**attempt)) + random.uniform(0, 0.15))
            except _PlayerAuthenticationError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError, ValueError):
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep((0.35 * (2**attempt)) + random.uniform(0, 0.15))
        raise AssertionError("unreachable")

    async def _mark_refresh(self, context: _Context, state: str) -> None:
        now = datetime.now(UTC).isoformat()

        def operation(db: sqlite3.Connection) -> None:
            db.execute(
                "INSERT INTO refresh_states (refresh_kind, region_id, uid, updated_at) "
                "VALUES ('player', ?, ?, ?) ON CONFLICT(refresh_kind, region_id, uid) "
                "DO UPDATE SET updated_at = excluded.updated_at",
                (context.region_id, context.uid, now),
            )
            if state == "attempt":
                db.execute(
                    "UPDATE refresh_states SET last_attempt_at = ?, updated_at = ? "
                    "WHERE refresh_kind = 'player' AND region_id = ? AND uid = ?",
                    (now, now, context.region_id, context.uid),
                )
            elif state == "success":
                db.execute(
                    "UPDATE refresh_states SET last_success_at = ?, failure_count = 0, "
                    "backoff_until = NULL, last_error_category = NULL, updated_at = ? "
                    "WHERE refresh_kind = 'player' AND region_id = ? AND uid = ?",
                    (now, now, context.region_id, context.uid),
                )
                db.execute(
                    "UPDATE credentials SET game_token_status = 'valid', updated_at = ? "
                    "WHERE credential_id = (SELECT credential_id FROM game_accounts "
                    "WHERE region_id = ? AND uid = ?)",
                    (now, context.region_id, context.uid),
                )
            else:
                db.execute(
                    "UPDATE refresh_states SET failure_count = failure_count + 1, "
                    "last_error_category = 'upstream', updated_at = ? "
                    "WHERE refresh_kind = 'player' AND region_id = ? AND uid = ?",
                    (now, context.region_id, context.uid),
                )

        await self.database.write(operation)

    async def _mark_game_auth_invalid(self, context: _Context) -> None:
        now = datetime.now(UTC).isoformat()
        await self.database.write(
            lambda db: db.execute(
                "UPDATE credentials SET game_token_status = 'needs_login', updated_at = ? "
                "WHERE credential_id = (SELECT credential_id FROM game_accounts "
                "WHERE region_id = ? AND uid = ? AND qq_id = ?)",
                (now, context.region_id, context.uid, context.qq_id),
            )
        )

    def _inside_cooldown(self, refreshed_at: str) -> bool:
        try:
            refreshed = datetime.fromisoformat(refreshed_at)
        except ValueError:
            return False
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - refreshed.astimezone(UTC)).total_seconds()
        return age < self.settings.player_refresh_cooldown_seconds

    def _write_raw_snapshot(
        self,
        context: _Context,
        player_response: object,
        role_response: object,
    ) -> None:
        if self.raw_snapshot_directory is None:
            return
        self.raw_snapshot_directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(f"{context.region_id}\0{context.uid}\0player".encode()).hexdigest()
        target = self.raw_snapshot_directory / f"{key}.json.enc"
        temporary = target.with_suffix(".tmp")
        payload = {
            "region_id": context.region_id,
            "uid": context.uid,
            "player": self._redact(player_response),
            "role": self._redact(role_response),
        }
        encrypted = self.cipher.encrypt_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        temporary.write_text(encrypted, encoding="utf-8")
        temporary.replace(target)

    @classmethod
    def _redact(cls, value: object) -> object:
        blocked = {"email", "token", "oauth", "cookie", "authorization", "device"}
        if isinstance(value, dict):
            return {
                str(key): cls._redact(item)
                for key, item in value.items()
                if not any(word in str(key).casefold() for word in blocked)
            }
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        return value

    async def _context(
        self,
        qq_id: str,
        *,
        external_query: bool,
        uid: str | None,
        region_id: str | None,
    ) -> _Context:
        def operation(db: sqlite3.Connection) -> _Context:
            user = db.execute(
                "SELECT default_region_id, default_uid, active_profile_id "
                "FROM users WHERE qq_id = ?",
                (qq_id,),
            ).fetchone()
            if user is None:
                raise PlayerDataError("尚未绑定国际服 UID，请先执行 /kh 登录")
            if uid:
                target_uid = uid
                target_region_id = str(region_id or "")
            elif external_query:
                target_uid = str(user["default_uid"] or "")
                target_region_id = str(user["default_region_id"] or "")
            else:
                active = db.execute(
                    "SELECT region_id, uid FROM profiles "
                    "WHERE profile_id = ? AND profile_type = 'uid'",
                    (user["active_profile_id"],),
                ).fetchone()
                target_uid = str(active["uid"] or "") if active else ""
                target_region_id = str(active["region_id"] or "") if active else ""
            if not target_uid:
                raise PlayerDataError("当前活动档案不是国际服 UID，请先执行 /kh 切换 <UID>")
            if not target_region_id:
                active = db.execute(
                    "SELECT region_id, uid FROM profiles "
                    "WHERE profile_id = ? AND profile_type = 'uid'",
                    (user["active_profile_id"],),
                ).fetchone()
                if active is not None and str(active["uid"]) == target_uid:
                    target_region_id = str(active["region_id"])
            if not target_region_id:
                matches = db.execute(
                    "SELECT region_id FROM game_accounts WHERE qq_id = ? AND uid = ? "
                    "ORDER BY region_id",
                    (qq_id, target_uid),
                ).fetchall()
                if len(matches) > 1:
                    raise PlayerDataError("该 UID 绑定了多个区服，请先切换到目标账号")
                target_region_id = str(matches[0]["region_id"]) if matches else ""
            row = db.execute(
                "SELECT g.uid, g.region_id, g.region_name, g.credential_id, "
                "c.encrypted_tokens "
                "FROM game_accounts g JOIN credentials c ON c.credential_id = g.credential_id "
                "WHERE g.qq_id = ? AND g.region_id = ? AND g.uid = ?",
                (qq_id, target_region_id, target_uid),
            ).fetchone()
            if row is None:
                raise PlayerDataError("未找到绑定的国际服 UID")
            return _Context(
                qq_id,
                str(row["uid"]),
                str(row["region_id"]),
                str(row["region_name"]),
                int(row["credential_id"]),
                str(row["encrypted_tokens"]),
            )

        return await self.database.read(operation)

    @classmethod
    def _select_player(
        cls,
        payload: object,
        uid: str,
        region_id: str,
    ) -> tuple[str, dict[str, object]]:
        data = cls._response_data(payload)
        matches: list[tuple[str, dict[str, object]]] = []
        for response_region, raw in data.items():
            if not regions_equivalent(response_region, region_id):
                continue
            player = cls._nested_json(raw)
            if str(player.get("roleId") or "") == uid:
                matches.append((response_region, player))
        if len(matches) == 1:
            return matches[0]
        raise ValueError("玩家信息响应中未找到目标 UID")

    @classmethod
    def _select_role_detail(cls, payload: object, region_id: str) -> dict[str, object]:
        data = cls._response_data(payload)
        raw = data.get(region_id)
        if raw is None:
            matches = tuple(
                value for key, value in data.items() if regions_equivalent(key, region_id)
            )
            if len(matches) != 1:
                raise ValueError("玩家详情响应中未找到目标区服")
            raw = matches[0]
        detail = cls._nested_json(raw)
        return detail

    @staticmethod
    def _response_data(payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("国际服玩家接口响应格式无效")
        if PlayerDataService._integer(payload.get("code")) != 0:
            raise _PlayerSessionRejected("国际服游戏接口拒绝了当前数据请求")
        data = payload.get("data")
        if not isinstance(data, dict) or not data:
            raise ValueError("国际服玩家接口响应格式无效")
        return data

    @staticmethod
    def _nested_json(value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            raise ValueError("国际服玩家接口嵌套数据格式无效")
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("国际服玩家接口嵌套数据格式无效")
        return parsed

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row | None) -> PlayerSnapshot | None:
        if row is None:
            return None
        return PlayerSnapshot(
            uid=str(row["uid"]),
            region_id=str(row["region_id"]),
            region_name=str(row["region_name"]),
            player_name=row["player_name"],
            head_photo=row["head_photo"],
            level=row["level"],
            world_level=row["world_level"],
            role_num=row["role_num"],
            active_days=row["active_days"],
            created_at_ms=row["created_at_ms"],
            energy=row["energy"],
            max_energy=row["max_energy"],
            store_energy=row["store_energy"],
            max_store_energy=row["max_store_energy"],
            energy_recover_time_ms=row["energy_recover_time_ms"],
            store_energy_recover_time_ms=row["store_energy_recover_time_ms"],
            liveness=row["liveness"],
            liveness_max=row["liveness_max"],
            liveness_unlock=(
                bool(row["liveness_unlock"]) if row["liveness_unlock"] is not None else None
            ),
            weekly_inst_count=row["weekly_inst_count"],
            battle_pass_present=bool(row["battle_pass_present"]),
            battle_pass_level=row["battle_pass_level"],
            battle_pass_week_exp=row["battle_pass_week_exp"],
            battle_pass_week_max_exp=row["battle_pass_week_max_exp"],
            battle_pass_is_unlock=(
                bool(row["battle_pass_is_unlock"])
                if row["battle_pass_is_unlock"] is not None
                else None
            ),
            battle_pass_is_open=(
                bool(row["battle_pass_is_open"]) if row["battle_pass_is_open"] is not None else None
            ),
            battle_pass_exp=row["battle_pass_exp"],
            battle_pass_exp_limit=row["battle_pass_exp_limit"],
            sound_box=row["sound_box"],
            boxes=PlayerDataService._collection(row["boxes_json"]),
            basic_boxes=PlayerDataService._collection(row["basic_boxes_json"]),
            phantom_boxes=PlayerDataService._collection(row["phantom_boxes_json"]),
            refreshed_at=str(row["refreshed_at"]),
        )

    @staticmethod
    def _collection_json(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        normalized = {
            str(key): number
            for key, raw in value.items()
            if (number := PlayerDataService._integer(raw)) is not None
        }
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _collection(value: object) -> tuple[tuple[str, int], ...] | None:
        if value is None:
            return None
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return tuple(
            sorted(
                ((str(key), int(number)) for key, number in parsed.items()),
                key=lambda item: (0, int(item[0])) if item[0].isdigit() else (1, item[0]),
            )
        )

    @staticmethod
    def _integer(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _boolean(value: object) -> bool | None:
        return value if isinstance(value, bool) else None

    @staticmethod
    def _boolean_integer(value: object) -> int | None:
        result = PlayerDataService._boolean(value)
        return int(result) if result is not None else None

    @staticmethod
    def _text(value: object) -> str | None:
        result = str(value or "").strip()
        return result or None
