"""从国际服启动器接口刷新并持久化唯一玩家详情快照。"""

import asyncio
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import aiohttp

from ..domain.player import PlayerDataError, PlayerSnapshot
from ..infrastructure.crypto import CryptoError, TokenCipher
from ..infrastructure.database import Database
from ..infrastructure.http import HttpClient

_PLAYER_INFO_URL = "https://pc-launcher-sdk-api.kurogame.net/game/queryPlayerInfo"
_ROLE_URL = "https://pc-launcher-sdk-api.kurogame.net/game/queryRole"
_ALLOWED_HOSTS = {"pc-launcher-sdk-api.kurogame.net"}
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Context:
    qq_id: str
    uid: str
    region_id: str
    region_name: str
    encrypted_tokens: str


class PlayerDataService:
    def __init__(self, database: Database, cipher: TokenCipher, http: HttpClient):
        self.database = database
        self.cipher = cipher
        self.http = http
        self._guard = asyncio.Lock()
        self._flights: dict[str, asyncio.Task[PlayerSnapshot]] = {}

    async def query(
        self,
        qq_id: str,
        *,
        external_query: bool = False,
        uid: str | None = None,
    ) -> PlayerSnapshot:
        context = await self._context(qq_id, external_query=external_query, uid=uid)
        async with self._guard:
            task = self._flights.get(context.uid)
            if task is None or task.done():
                task = asyncio.create_task(self._refresh(context))
                self._flights[context.uid] = task
        try:
            return await asyncio.shield(task)
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
            cached = await self.cached(qq_id, external_query=external_query, uid=context.uid)
            if cached is not None:
                return replace(cached, is_cached_fallback=True)
            raise PlayerDataError("玩家详情刷新失败，且本地没有可用缓存，请重新登录后重试") from exc
        finally:
            if task.done():
                async with self._guard:
                    if self._flights.get(context.uid) is task:
                        self._flights.pop(context.uid, None)

    async def cached(
        self,
        qq_id: str,
        *,
        external_query: bool = False,
        uid: str | None = None,
    ) -> PlayerSnapshot | None:
        context = await self._context(qq_id, external_query=external_query, uid=uid)
        return await self.database.read(
            lambda db: self._snapshot_from_row(
                db.execute(
                    "SELECT s.*, g.region_id, g.region_name FROM player_snapshots s "
                    "JOIN game_accounts g ON g.uid = s.uid WHERE s.uid = ? AND g.qq_id = ?",
                    (context.uid, context.qq_id),
                ).fetchone()
            )
        )

    async def _refresh(self, context: _Context) -> PlayerSnapshot:
        sensitive = json.loads(self.cipher.decrypt_text(context.encrypted_tokens))
        oauth_code = str(sensitive.get("oauth_code") or "").strip()
        if not oauth_code:
            raise PlayerDataError("账号授权信息缺失，请重新执行 /kh 登录")

        player_response = await self.http.post_json(
            _PLAYER_INFO_URL,
            {"oauthCode": oauth_code},
            allowed_hosts=_ALLOWED_HOSTS,
            max_bytes=_MAX_RESPONSE_BYTES,
        )
        region_id, player = self._select_player(player_response, context.uid)
        role_response = await self.http.post_json(
            _ROLE_URL,
            {"oauthCode": oauth_code, "playerId": int(context.uid), "region": region_id},
            allowed_hosts=_ALLOWED_HOSTS,
            max_bytes=_MAX_RESPONSE_BYTES,
        )
        base = self._select_role_base(role_response, region_id)
        refreshed_at = datetime.now(UTC).isoformat()
        values = {
            "player_name": self._text(player.get("roleName")) or self._text(base.get("Name")),
            "head_photo": self._integer(player.get("headPhoto")),
            "level": self._integer(base.get("Level")) or self._integer(player.get("level")),
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
            "sound_box": self._integer(base.get("SoundBox")),
            "boxes_json": self._collection_json(base.get("Boxes")),
            "basic_boxes_json": self._collection_json(base.get("BasicBoxes")),
            "phantom_boxes_json": self._collection_json(base.get("PhantomBoxes")),
        }

        def operation(db: sqlite3.Connection) -> PlayerSnapshot:
            columns = tuple(values)
            db.execute(
                f"INSERT INTO player_snapshots (uid, {', '.join(columns)}, refreshed_at) "
                f"VALUES (?, {', '.join('?' for _ in columns)}, ?) "
                "ON CONFLICT(uid) DO UPDATE SET "
                + ", ".join(f"{column} = excluded.{column}" for column in columns)
                + ", refreshed_at = excluded.refreshed_at",
                (context.uid, *(values[column] for column in columns), refreshed_at),
            )
            db.execute(
                "UPDATE game_accounts SET player_name = COALESCE(?, player_name) "
                "WHERE uid = ? AND qq_id = ?",
                (values["player_name"], context.uid, context.qq_id),
            )
            row = db.execute(
                "SELECT s.*, g.region_id, g.region_name FROM player_snapshots s "
                "JOIN game_accounts g ON g.uid = s.uid WHERE s.uid = ?",
                (context.uid,),
            ).fetchone()
            snapshot = self._snapshot_from_row(row)
            if snapshot is None:
                raise PlayerDataError("玩家详情快照写入失败")
            return snapshot

        return await self.database.write(operation)

    async def _context(
        self,
        qq_id: str,
        *,
        external_query: bool,
        uid: str | None,
    ) -> _Context:
        def operation(db: sqlite3.Connection) -> _Context:
            user = db.execute(
                "SELECT default_uid, active_profile_id FROM users WHERE qq_id = ?", (qq_id,)
            ).fetchone()
            if user is None:
                raise PlayerDataError("尚未绑定国际服 UID，请先执行 /kh 登录")
            if uid:
                target_uid = uid
            elif external_query:
                target_uid = str(user["default_uid"] or "")
            else:
                active = db.execute(
                    "SELECT uid FROM profiles WHERE profile_id = ? AND profile_type = 'uid'",
                    (user["active_profile_id"],),
                ).fetchone()
                target_uid = str(active["uid"] or "") if active else ""
            if not target_uid:
                raise PlayerDataError("当前活动档案不是国际服 UID，请先执行 /kh 切换 <UID>")
            row = db.execute(
                "SELECT g.uid, g.region_id, g.region_name, c.encrypted_tokens "
                "FROM game_accounts g JOIN credentials c ON c.credential_id = g.credential_id "
                "WHERE g.qq_id = ? AND g.uid = ?",
                (qq_id, target_uid),
            ).fetchone()
            if row is None:
                raise PlayerDataError("未找到绑定的国际服 UID")
            return _Context(
                qq_id,
                str(row["uid"]),
                str(row["region_id"]),
                str(row["region_name"]),
                str(row["encrypted_tokens"]),
            )

        return await self.database.read(operation)

    @classmethod
    def _select_player(cls, payload: object, uid: str) -> tuple[str, dict[str, object]]:
        data = cls._response_data(payload)
        for region_id, raw in data.items():
            player = cls._nested_json(raw)
            if str(player.get("roleId") or "") == uid:
                return str(region_id), player
        raise ValueError("玩家信息响应中未找到目标 UID")

    @classmethod
    def _select_role_base(cls, payload: object, region_id: str) -> dict[str, object]:
        data = cls._response_data(payload)
        raw = data.get(region_id)
        if raw is None and len(data) == 1:
            raw = next(iter(data.values()))
        detail = cls._nested_json(raw)
        base = detail.get("Base")
        if not isinstance(base, dict):
            raise ValueError("玩家详情响应缺少 Base")
        return base

    @staticmethod
    def _response_data(payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or PlayerDataService._integer(payload.get("code")) != 0:
            raise ValueError("国际服玩家接口拒绝请求")
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
    def _text(value: object) -> str | None:
        result = str(value or "").strip()
        return result or None
