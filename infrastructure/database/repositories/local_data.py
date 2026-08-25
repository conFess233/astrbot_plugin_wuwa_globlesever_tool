"""纯本地档案与当前角色记录仓储。"""

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ....domain.catalog import CharacterDefinition
from ...storage import remove_profile_cards
from .. import Database


class LocalDataError(ValueError):
    """表示本地档案操作无法完成。"""


@dataclass(frozen=True, slots=True)
class CharacterRecord:
    profile_id: int
    character_id: str
    character_name: str
    record_origin: str
    level: int | None
    chain: int | None
    weapon_id: str | None
    weapon_name: str | None
    weapon_picture_url: str | None
    weapon_star: int | None
    weapon_type_id: str | None
    weapon_type_picture_url: str | None
    weapon_level: int | None
    weapon_refinement: int | None
    score_total: float | None
    score_grade: str | None
    level_source: str | None
    chain_source: str | None
    weapon_source: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class ProfileSelection:
    profile_id: int
    profile_type: str
    region_id: str | None
    uid: str | None

    @property
    def label(self) -> str:
        return (
            "纯本地"
            if self.profile_type == "local"
            else f"{self.region_id or '未知区服'} · UID {self.uid}"
        )


@dataclass(frozen=True, slots=True)
class PendingAction:
    action_type: str
    summary: str
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class LocalDataRepository:
    def __init__(
        self,
        database: Database,
        card_cache_directory: Path | None = None,
    ):
        self.database = database
        self.card_cache_directory = card_cache_directory

    async def active_profile(self, qq_id: str, *, external_query: bool = False) -> ProfileSelection:
        if external_query:
            result = await self.database.read(lambda db: self._external_profile(db, qq_id))
            if result is None:
                raise LocalDataError("该用户暂无可查询的本地数据")
            return result
        return await self.database.write(lambda db: self._ensure_active_profile(db, qq_id))

    async def list_characters(self, profile_id: int) -> list[CharacterRecord]:
        return await self.database.read(
            lambda db: [
                self._to_record(row)
                for row in db.execute(
                    "SELECT * FROM characters WHERE profile_id = ? "
                    "ORDER BY CASE WHEN api_source_order IS NULL THEN 1 ELSE 0 END, "
                    "api_source_order, character_name_snapshot COLLATE NOCASE, character_id",
                    (profile_id,),
                ).fetchall()
            ]
        )

    async def get_character(self, profile_id: int, character_id: str) -> CharacterRecord | None:
        return await self.database.read(
            lambda db: self._optional_record(
                db.execute(
                    "SELECT * FROM characters WHERE profile_id = ? AND character_id = ?",
                    (profile_id, character_id),
                ).fetchone()
            )
        )

    async def role_snapshot_updated_at(self, profile_id: int) -> str | None:
        return await self.database.read(
            lambda db: (
                lambda row: (
                    str(row["last_sync_success_at"])
                    if row is not None and row["last_sync_success_at"]
                    else None
                )
            )(
                db.execute(
                    "SELECT g.last_sync_success_at FROM profiles p "
                    "JOIN game_accounts g ON g.region_id = p.region_id AND g.uid = p.uid "
                    "WHERE p.profile_id = ?",
                    (profile_id,),
                ).fetchone()
            )
        )

    async def set_manual_field(
        self,
        qq_id: str,
        character: CharacterDefinition,
        field: str,
        value: int | str | None,
    ) -> CharacterRecord:
        columns = {
            "等级": "manual_level",
            "共鸣链": "manual_chain",
            "武器": "manual_weapon_id",
            "武器等级": "manual_weapon_level",
            "武器精炼": "manual_weapon_refinement",
        }
        column = columns.get(field)
        if column is None:
            raise LocalDataError("不支持的修改字段")

        def operation(db: sqlite3.Connection) -> CharacterRecord:
            profile = self._ensure_active_profile(db, qq_id)
            now = _iso()
            db.execute(
                "INSERT INTO characters (profile_id, character_id, character_name_snapshot, "
                "record_origin, updated_at, last_manual_update_at) "
                "VALUES (?, ?, ?, 'manual', ?, ?) "
                "ON CONFLICT(profile_id, character_id) DO UPDATE SET "
                "character_name_snapshot = excluded.character_name_snapshot, "
                "record_origin = CASE WHEN characters.record_origin = 'api' THEN 'mixed' "
                "ELSE characters.record_origin END, updated_at = excluded.updated_at, "
                "last_manual_update_at = excluded.last_manual_update_at",
                (profile.profile_id, character.character_id, character.display_name, now, now),
            )
            db.execute(
                f"UPDATE characters SET {column} = ?, updated_at = ?, "
                "last_manual_update_at = ? WHERE profile_id = ? AND character_id = ?",
                (value, now, now, profile.profile_id, character.character_id),
            )
            row = db.execute(
                "SELECT * FROM characters WHERE profile_id = ? AND character_id = ?",
                (profile.profile_id, character.character_id),
            ).fetchone()
            return self._to_record(row)

        return await self.database.write(operation)

    async def reset_manual_fields(
        self,
        qq_id: str,
        character_id: str,
        field: str,
    ) -> CharacterRecord | None:
        columns = {
            "等级": ("manual_level",),
            "共鸣链": ("manual_chain",),
            "武器": ("manual_weapon_id", "manual_weapon_level", "manual_weapon_refinement"),
            "武器等级": ("manual_weapon_level",),
            "武器精炼": ("manual_weapon_refinement",),
            "全部": (
                "manual_level",
                "manual_chain",
                "manual_weapon_id",
                "manual_weapon_level",
                "manual_weapon_refinement",
            ),
        }
        selected = columns.get(field)
        if selected is None:
            raise LocalDataError("不支持的重置字段")

        def operation(db: sqlite3.Connection) -> CharacterRecord | None:
            profile = self._ensure_active_profile(db, qq_id)
            assignments = ", ".join(f"{column} = NULL" for column in selected)
            db.execute(
                f"UPDATE characters SET {assignments}, updated_at = ?, last_manual_update_at = ? "
                "WHERE profile_id = ? AND character_id = ?",
                (_iso(), _iso(), profile.profile_id, character_id),
            )
            row = db.execute(
                "SELECT * FROM characters WHERE profile_id = ? AND character_id = ?",
                (profile.profile_id, character_id),
            ).fetchone()
            if row is None:
                return None
            has_manual = any(
                row[column] is not None
                for column in (
                    "manual_level",
                    "manual_chain",
                    "manual_weapon_id",
                    "manual_weapon_level",
                    "manual_weapon_refinement",
                )
            )
            if row["record_origin"] == "manual" and not has_manual:
                db.execute(
                    "DELETE FROM characters WHERE profile_id = ? AND character_id = ?",
                    (profile.profile_id, character_id),
                )
                return None
            if row["record_origin"] == "mixed" and not has_manual:
                db.execute(
                    "UPDATE characters SET record_origin = 'api' "
                    "WHERE profile_id = ? AND character_id = ?",
                    (profile.profile_id, character_id),
                )
                row = db.execute(
                    "SELECT * FROM characters WHERE profile_id = ? AND character_id = ?",
                    (profile.profile_id, character_id),
                ).fetchone()
            return self._to_record(row)

        return await self.database.write(operation)

    async def set_language(self, qq_id: str, language: str) -> None:
        def operation(db: sqlite3.Connection) -> None:
            self._ensure_active_profile(db, qq_id)
            db.execute(
                "UPDATE users SET language = ?, updated_at = ? WHERE qq_id = ?",
                (language, _iso(), qq_id),
            )

        await self.database.write(operation)

    async def switch_local(self, qq_id: str) -> ProfileSelection:
        def operation(db: sqlite3.Connection) -> ProfileSelection:
            self._ensure_active_profile(db, qq_id)
            row = db.execute(
                "SELECT profile_id FROM profiles WHERE qq_id = ? AND profile_type = 'local'",
                (qq_id,),
            ).fetchone()
            local_id = int(row["profile_id"])
            db.execute(
                "UPDATE users SET active_profile_id = ?, updated_at = ? WHERE qq_id = ?",
                (local_id, _iso(), qq_id),
            )
            return ProfileSelection(local_id, "local", None, None)

        return await self.database.write(operation)

    async def begin_character_delete(
        self,
        qq_id: str,
        character_id: str,
        origin_context: str,
    ) -> PendingAction:
        profile = await self.active_profile(qq_id)
        record = await self.get_character(profile.profile_id, character_id)
        if record is None:
            raise LocalDataError("当前档案中没有该角色记录")
        if record.record_origin != "manual":
            raise LocalDataError("接口拥有角色不能删除，只能重置手动字段")
        return await self._begin_action(
            qq_id,
            "character_delete",
            {"profile_id": profile.profile_id, "character_id": character_id},
            origin_context,
            f"删除纯本地角色 {record.character_name}",
        )

    async def begin_reset_all(
        self,
        qq_id: str,
        character_id: str,
        character_name: str,
        origin_context: str,
    ) -> PendingAction:
        profile = await self.active_profile(qq_id)
        record = await self.get_character(profile.profile_id, character_id)
        if record is None:
            raise LocalDataError("当前档案中没有该角色记录")
        return await self._begin_action(
            qq_id,
            "reset_all",
            {"profile_id": profile.profile_id, "character_id": character_id},
            origin_context,
            f"重置 {character_name} 的全部手动字段",
        )

    async def confirm(self, qq_id: str, origin_context: str) -> str:
        now = _now()

        def operation(db: sqlite3.Connection) -> tuple[str, tuple[int, ...]]:
            matched = db.execute(
                "SELECT * FROM pending_actions WHERE qq_id = ? AND used_at IS NULL "
                "AND expires_at > ? AND origin_context = ? AND action_type != 'uid_unbind' "
                "ORDER BY created_at DESC LIMIT 1",
                (qq_id, _iso(now), origin_context),
            ).fetchone()
            if matched is None:
                raise LocalDataError("当前会话没有待确认操作，或操作已过期")

            payload = json.loads(matched["payload_json"])
            action_type = str(matched["action_type"])
            db.execute(
                "UPDATE pending_actions SET used_at = ? WHERE action_id = ? AND used_at IS NULL",
                (_iso(now), matched["action_id"]),
            )
            if action_type == "character_delete":
                db.execute(
                    "DELETE FROM characters WHERE profile_id = ? AND character_id = ?",
                    (payload["profile_id"], payload["character_id"]),
                )
                return "角色记录已删除", (int(payload["profile_id"]),)
            if action_type == "reset_all":
                db.execute(
                    "UPDATE characters SET manual_level = NULL, manual_chain = NULL, "
                    "manual_weapon_id = NULL, manual_weapon_level = NULL, "
                    "manual_weapon_refinement = NULL, last_manual_update_at = ?, updated_at = ? "
                    "WHERE profile_id = ? AND character_id = ?",
                    (
                        _iso(now),
                        _iso(now),
                        payload["profile_id"],
                        payload["character_id"],
                    ),
                )
                row = db.execute(
                    "SELECT record_origin FROM characters "
                    "WHERE profile_id = ? AND character_id = ?",
                    (payload["profile_id"], payload["character_id"]),
                ).fetchone()
                if row is not None and str(row["record_origin"]) == "manual":
                    db.execute(
                        "DELETE FROM characters WHERE profile_id = ? AND character_id = ?",
                        (payload["profile_id"], payload["character_id"]),
                    )
                elif row is not None and str(row["record_origin"]) == "mixed":
                    db.execute(
                        "UPDATE characters SET record_origin = 'api' "
                        "WHERE profile_id = ? AND character_id = ?",
                        (payload["profile_id"], payload["character_id"]),
                    )
                return "角色的全部手动字段已重置", (int(payload["profile_id"]),)
            raise LocalDataError("确认操作类型不受支持")

        message, profile_ids = await self.database.write(operation)
        await asyncio.to_thread(remove_profile_cards, self.card_cache_directory, profile_ids)
        return message

    async def cancel(self, qq_id: str, origin_context: str) -> str:
        if not origin_context:
            raise LocalDataError("当前会话无法安全取消操作")

        def operation(db: sqlite3.Connection) -> int:
            cursor = db.execute(
                "DELETE FROM pending_actions WHERE qq_id = ? AND used_at IS NULL "
                "AND expires_at > ? AND origin_context = ?",
                (qq_id, _iso(), origin_context),
            )
            return int(cursor.rowcount)

        removed = await self.database.write(operation)
        return "已取消待确认操作" if removed else "当前会话没有待确认操作"

    async def _begin_action(
        self,
        qq_id: str,
        action_type: str,
        payload: dict[str, object],
        origin_context: str,
        summary: str,
    ) -> PendingAction:
        if not origin_context:
            raise LocalDataError("当前会话无法安全绑定确认操作")
        action_id = uuid.uuid4().hex
        expires_at = _now() + timedelta(seconds=60)

        def operation(db: sqlite3.Connection) -> None:
            db.execute("DELETE FROM pending_actions WHERE qq_id = ?", (qq_id,))
            db.execute(
                "INSERT INTO pending_actions (action_id, qq_id, action_type, payload_json, "
                "confirm_code_hash, expires_at, created_at, origin_context, summary) "
                "VALUES (?, ?, ?, ?, '', ?, ?, ?, ?)",
                (
                    action_id,
                    qq_id,
                    action_type,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    _iso(expires_at),
                    _iso(),
                    origin_context,
                    summary,
                ),
            )

        await self.database.write(operation)
        return PendingAction(action_type, summary, expires_at)

    @staticmethod
    def _ensure_active_profile(db: sqlite3.Connection, qq_id: str) -> ProfileSelection:
        now = _iso()
        db.execute(
            "INSERT INTO users (qq_id, created_at, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(qq_id) DO UPDATE SET updated_at = excluded.updated_at",
            (qq_id, now, now),
        )
        db.execute(
            "INSERT OR IGNORE INTO profiles (qq_id, profile_type, uid, updated_at) "
            "VALUES (?, 'local', NULL, ?)",
            (qq_id, now),
        )
        local_id = int(
            db.execute(
                "SELECT profile_id FROM profiles WHERE qq_id = ? AND profile_type = 'local'",
                (qq_id,),
            ).fetchone()[0]
        )
        db.execute(
            "UPDATE users SET active_profile_id = COALESCE(active_profile_id, ?) WHERE qq_id = ?",
            (local_id, qq_id),
        )
        row = db.execute(
            "SELECT p.profile_id, p.profile_type, p.region_id, p.uid FROM users u "
            "JOIN profiles p ON p.profile_id = u.active_profile_id WHERE u.qq_id = ?",
            (qq_id,),
        ).fetchone()
        return ProfileSelection(
            int(row["profile_id"]),
            str(row["profile_type"]),
            row["region_id"],
            row["uid"],
        )

    @staticmethod
    def _external_profile(db: sqlite3.Connection, qq_id: str) -> ProfileSelection | None:
        row = db.execute(
            "SELECT p.profile_id, p.profile_type, p.region_id, p.uid FROM users u "
            "JOIN profiles p ON p.qq_id = u.qq_id "
            "WHERE u.qq_id = ? AND (("
            "u.default_region_id IS NOT NULL AND u.default_uid IS NOT NULL "
            "AND p.region_id = u.default_region_id AND p.uid = u.default_uid) "
            "OR (u.default_uid IS NULL AND p.profile_type = 'local')) "
            "ORDER BY CASE WHEN p.region_id = u.default_region_id "
            "AND p.uid = u.default_uid THEN 0 ELSE 1 END LIMIT 1",
            (qq_id,),
        ).fetchone()
        if row is None:
            return None
        return ProfileSelection(
            int(row["profile_id"]),
            str(row["profile_type"]),
            row["region_id"],
            row["uid"],
        )

    @staticmethod
    def _optional_record(row: sqlite3.Row | None) -> CharacterRecord | None:
        return None if row is None else LocalDataRepository._to_record(row)

    @staticmethod
    def _to_record(row: sqlite3.Row) -> CharacterRecord:
        api_weapon_present = row["api_weapon_present"]
        api_weapon_used = api_weapon_present == 1 and row["api_weapon_id"] is not None
        weapon_id = row["api_weapon_id"] if api_weapon_used else row["manual_weapon_id"]
        return CharacterRecord(
            profile_id=int(row["profile_id"]),
            character_id=str(row["character_id"]),
            character_name=str(row["character_name_snapshot"]),
            record_origin=str(row["record_origin"]),
            level=row["manual_level"] if row["manual_level"] is not None else row["api_level"],
            chain=row["manual_chain"] if row["manual_chain"] is not None else row["api_chain"],
            weapon_id=weapon_id,
            weapon_name=(row["api_weapon_name"] if api_weapon_used else row["manual_weapon_id"]),
            weapon_picture_url=row["api_weapon_picture_url"] if api_weapon_used else None,
            weapon_star=row["api_weapon_star"] if api_weapon_used else None,
            weapon_type_id=row["api_weapon_type_id"] if api_weapon_used else None,
            weapon_type_picture_url=(
                row["api_weapon_type_picture_url"] if api_weapon_used else None
            ),
            weapon_level=row["manual_weapon_level"] if weapon_id else None,
            weapon_refinement=row["manual_weapon_refinement"] if weapon_id else None,
            score_total=row["score_total"],
            score_grade=row["score_grade"],
            level_source=(
                "manual"
                if row["manual_level"] is not None
                else "api"
                if row["api_level"] is not None
                else None
            ),
            chain_source=(
                "manual"
                if row["manual_chain"] is not None
                else "api"
                if row["api_chain"] is not None
                else None
            ),
            weapon_source=(
                "api"
                if api_weapon_used
                else "manual"
                if row["manual_weapon_id"] is not None
                else None
            ),
            updated_at=str(row["updated_at"]),
        )
