"""攻略站角色快照同步、单飞并发控制与原子写入。"""

import asyncio
import contextlib
import json
import random
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from ...domain.catalog import CatalogError, CharacterCatalog
from ...domain.models import RegionUid
from ...domain.sync import (
    GuideAuthenticationError,
    GuideAvatar,
    GuideError,
    GuideRoleDetail,
    GuideSyncClient,
    GuideUnavailableError,
    SyncedCharacter,
    SyncResult,
)
from ...infrastructure.database import Database
from ...infrastructure.security import CryptoError, TokenCipher
from ..settings import PluginSettings
from .coordinator import SingleFlightCoordinator

_T = TypeVar("_T")
_LANGUAGE_MAP = {
    "zh-CN": "zh-Hans",
    "zh-TW": "zh-Hant",
    "en": "en",
    "ja": "ja",
    "ko": "ko",
}


class SyncError(ValueError):
    """表示可安全展示的同步失败。"""


class SyncCooldownError(SyncError):
    """表示用户主动刷新仍处于账号级冷却。"""


@dataclass(frozen=True, slots=True)
class _SyncContext:
    qq_id: str
    uid: str
    region_id: str
    language: str
    credential_id: int
    encrypted_tokens: str


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class GuideSyncService:
    def __init__(
        self,
        database: Database,
        cipher: TokenCipher,
        client: GuideSyncClient,
        catalog: CharacterCatalog,
        settings: PluginSettings,
        credential_invalidated: Callable[[str, str], Awaitable[None]] | None = None,
    ):
        self.database = database
        self.cipher = cipher
        self.client = client
        self.catalog = catalog
        self.settings = settings
        self.credential_invalidated = credential_invalidated
        self._global_gate = asyncio.Semaphore(settings.sync_concurrency)
        self._singleflight = SingleFlightCoordinator[SyncResult]()
        self._auto_task: asyncio.Task[None] | None = None

    async def sync(
        self,
        qq_id: str,
        uid: str | None = None,
        region_id: str | None = None,
        *,
        background: bool = False,
        force: bool = False,
    ) -> SyncResult:
        context = await self._context(qq_id, uid, region_id)
        if not background and not force:
            await self._check_user_cooldown(context)
        flight_key = RegionUid(context.region_id, context.uid).cache_key
        result = await self._singleflight.run(flight_key, lambda: self._run(context))
        if not background:
            await self._mark_user_success(context)
        return result

    def start_auto_sync(self) -> None:
        if self.settings.auto_sync_enabled and (self._auto_task is None or self._auto_task.done()):
            self._auto_task = asyncio.create_task(self._auto_loop())

    async def update_settings(self, settings: PluginSettings) -> None:
        old_task = self._auto_task
        self.settings = settings
        self._global_gate = asyncio.Semaphore(settings.sync_concurrency)
        if not settings.auto_sync_enabled and old_task is not None:
            self._auto_task = None
            old_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await old_task
        elif settings.auto_sync_enabled:
            self.start_auto_sync()

    @property
    def auto_sync_running(self) -> bool:
        return self._auto_task is not None and not self._auto_task.done()

    async def close(self) -> None:
        task, self._auto_task = self._auto_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._singleflight.wait()

    async def _run(self, context: _SyncContext) -> SyncResult:
        async with self._global_gate:
            await self._mark_attempt(context)
            try:
                async with asyncio.timeout(self.settings.role_refresh_timeout_seconds):
                    result = await self._fetch_and_normalize(context)
                    committed = await self._commit(context, *result)
                await self._mark_success(context)
                return committed
            except GuideAuthenticationError as exc:
                origin = await self._mark_failure(context, "needs_login", "authentication")
                if origin and self.credential_invalidated is not None:
                    with contextlib.suppress(Exception):
                        await self.credential_invalidated(context.qq_id, origin)
                raise SyncError("登录状态已失效，请重新执行 /kh 登录") from exc
            except (GuideUnavailableError, asyncio.TimeoutError) as exc:
                await self._mark_failure(context, "failed", "network")
                raise SyncError("攻略站暂时不可用，已保留上次同步数据") from exc
            except (
                GuideError,
                CatalogError,
                CryptoError,
                KeyError,
                TypeError,
                json.JSONDecodeError,
            ) as exc:
                await self._mark_failure(context, "failed", "invalid_data")
                raise SyncError("攻略站数据校验失败，已保留上次同步数据") from exc

    async def _fetch_and_normalize(
        self, context: _SyncContext
    ) -> tuple[tuple[GuideAvatar, ...], tuple[SyncedCharacter, ...]]:
        sensitive = json.loads(self.cipher.decrypt_text(context.encrypted_tokens))
        token = str(sensitive.get("guide_token") or "")
        if not token:
            token = await self._refresh_guide_token(context, sensitive)
        try:
            players = await self._retry(lambda: self.client.players(token, context.language))
        except GuideAuthenticationError:
            token = await self._refresh_guide_token(context, sensitive)
            players = await self._retry(lambda: self.client.players(token, context.language))
        player = next((item for item in players if item.uid == context.uid), None)
        if player is None or player.region_id != context.region_id:
            raise GuideError("攻略站账号中未找到绑定的 UID")
        await self.client.choose_player(token, context.language, context.uid, context.region_id)
        avatars = await self._retry(lambda: self.client.avatars(token, context.language))
        owned = tuple(avatar for avatar in avatars if avatar.is_acquired)
        role_gate = asyncio.Semaphore(self.settings.role_detail_concurrency)

        async def load(avatar: GuideAvatar) -> SyncedCharacter:
            async with role_gate:
                return await self._load_role(token, context.language, avatar)

        tasks = tuple(asyncio.create_task(load(avatar)) for avatar in owned)
        try:
            characters = tuple(await asyncio.gather(*tasks))
        finally:
            unfinished = tuple(task for task in tasks if not task.done())
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
        return avatars, characters

    async def _load_role(self, token: str, language: str, avatar: GuideAvatar) -> SyncedCharacter:
        definition = self.catalog.resolve(avatar.role_id)
        introductions = await self._retry(
            lambda: self.client.introductions(token, language, avatar.role_id)
        )
        preferred = sorted(
            introductions,
            key=lambda item: (
                0 if any(value.casefold() == language.casefold() for value in item.languages) else 1
            ),
        )
        selected: GuideRoleDetail | None = None
        for introduction in preferred:
            detail = await self._retry(
                lambda item=introduction: self.client.introduction_detail(
                    token, language, avatar.role_id, item.introduction_id
                )
            )
            if detail is None:
                continue
            selected = selected or detail
            if detail.chain is not None:
                selected = detail
                break
        if selected is None:
            raise GuideError(f"角色 {avatar.role_id} 的所有攻略方案均未返回详情")
        if selected.chain is not None and not 0 <= selected.chain <= 6:
            raise GuideError(f"角色 {avatar.role_id} 的共鸣链超出范围")
        if selected.weapon_present is True and not selected.weapon_id:
            raise GuideError(f"角色 {avatar.role_id} 的武器详情不完整")
        return SyncedCharacter(
            avatar.role_id,
            definition.display_name,
            selected.chain,
            selected.weapon_present,
            selected.weapon_id,
            selected.weapon_name,
            selected.weapon_picture_url,
            selected.weapon_star,
            selected.weapon_type_id,
            selected.weapon_type_picture_url,
            avatar.source_order,
        )

    async def _refresh_guide_token(
        self, context: _SyncContext, sensitive: dict[str, object]
    ) -> str:
        c_uid = str(sensitive.get("c_uid") or "")
        c_name = str(sensitive.get("c_name") or c_uid)
        access_token = str(sensitive.get("access_token") or "")
        if not c_uid or not access_token:
            raise GuideAuthenticationError("刷新攻略站登录状态所需凭据缺失")
        token = await self.client.login(c_uid, c_name, access_token, context.language)
        sensitive["guide_token"] = token
        encrypted = self.cipher.encrypt_text(
            json.dumps(sensitive, ensure_ascii=False, separators=(",", ":"))
        )
        await self.database.write(
            lambda db: db.execute(
                "UPDATE credentials SET encrypted_tokens = ?, guide_token_status = 'valid', "
                "updated_at = ? WHERE credential_id = ?",
                (encrypted, _iso(), context.credential_id),
            )
        )
        return token

    async def _retry(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        attempts = self.settings.request_retry_count + 1
        for attempt in range(attempts):
            try:
                return await operation()
            except GuideUnavailableError:
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep((0.4 * (2**attempt)) + random.uniform(0, 0.2))
        raise AssertionError("unreachable")

    async def _context(
        self,
        qq_id: str,
        uid: str | None,
        region_id: str | None,
    ) -> _SyncContext:
        def operation(db: sqlite3.Connection) -> _SyncContext:
            user = db.execute(
                "SELECT language, default_region_id, default_uid, active_profile_id "
                "FROM users WHERE qq_id = ?",
                (qq_id,),
            ).fetchone()
            if user is None:
                raise SyncError("尚未绑定国际服 UID，请先执行 /kh 登录")
            active = db.execute(
                "SELECT region_id, uid FROM profiles WHERE profile_id = ? AND profile_type = 'uid'",
                (user["active_profile_id"],),
            ).fetchone()
            target_uid = str(uid or (active["uid"] if active else "") or "").strip()
            target_region = str(region_id or "").strip()
            if not target_uid:
                raise SyncError("当前活动档案不是国际服 UID，请先执行 /kh 切换")
            if not target_region and active is not None and str(active["uid"]) == target_uid:
                target_region = str(active["region_id"])
            if not target_region and uid is None:
                target_region = str(user["default_region_id"] or "")
            if not target_region:
                matches = db.execute(
                    "SELECT region_id FROM game_accounts WHERE qq_id = ? AND uid = ? "
                    "ORDER BY region_id",
                    (qq_id, target_uid),
                ).fetchall()
                if len(matches) > 1:
                    raise SyncError("该 UID 绑定了多个区服，请先切换到目标账号")
                target_region = str(matches[0]["region_id"]) if matches else ""
            row = db.execute(
                "SELECT g.uid, g.region_id, g.credential_id, c.encrypted_tokens "
                "FROM game_accounts g JOIN credentials c ON c.credential_id = g.credential_id "
                "WHERE g.qq_id = ? AND g.region_id = ? AND g.uid = ?",
                (qq_id, target_region, target_uid),
            ).fetchone()
            if row is None:
                raise SyncError("未找到你绑定的该 UID")
            language = _LANGUAGE_MAP.get(str(user["language"]), "zh-Hans")
            return _SyncContext(
                qq_id,
                str(row["uid"]),
                str(row["region_id"]),
                language,
                int(row["credential_id"]),
                str(row["encrypted_tokens"]),
            )

        return await self.database.read(operation)

    async def _mark_attempt(self, context: _SyncContext) -> None:
        await self.database.write(
            lambda db: db.execute(
                "UPDATE game_accounts SET last_sync_attempt_at = ? WHERE region_id = ? AND uid = ?",
                (_iso(), context.region_id, context.uid),
            )
        )

    async def _mark_failure(self, context: _SyncContext, status: str, category: str) -> str | None:
        current = _now()
        now = _iso(current)

        def operation(db: sqlite3.Connection) -> str | None:
            db.execute(
                "UPDATE game_accounts SET sync_status = ?, last_sync_attempt_at = ?, "
                "last_error_category = ? "
                "WHERE region_id = ? AND uid = ? AND qq_id = ?",
                (
                    status,
                    now,
                    category,
                    context.region_id,
                    context.uid,
                    context.qq_id,
                ),
            )
            if status == "needs_login":
                db.execute(
                    "UPDATE credentials SET guide_token_status = 'needs_login', updated_at = ? "
                    "WHERE credential_id = ?",
                    (now, context.credential_id),
                )
                db.execute(
                    "UPDATE game_accounts SET sync_status = 'needs_login', "
                    "last_error_category = 'authentication' WHERE credential_id = ?",
                    (context.credential_id,),
                )
            row = db.execute(
                "SELECT failure_count FROM refresh_states WHERE refresh_kind = 'role' "
                "AND region_id = ? AND uid = ?",
                (context.region_id, context.uid),
            ).fetchone()
            failure_count = int(row["failure_count"]) + 1 if row else 1
            backoff_minutes = min(360, 5 * (2 ** min(failure_count - 1, 7)))
            backoff_until = _iso(current + timedelta(minutes=backoff_minutes))
            db.execute(
                "INSERT INTO refresh_states (refresh_kind, region_id, uid, last_attempt_at, "
                "failure_count, backoff_until, last_error_category, updated_at) "
                "VALUES ('role', ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(refresh_kind, region_id, uid) DO UPDATE SET "
                "last_attempt_at = excluded.last_attempt_at, "
                "failure_count = excluded.failure_count, "
                "backoff_until = excluded.backoff_until, "
                "last_error_category = excluded.last_error_category, "
                "updated_at = excluded.updated_at",
                (
                    context.region_id,
                    context.uid,
                    now,
                    failure_count,
                    backoff_until,
                    category,
                    now,
                ),
            )
            if status != "needs_login":
                return None
            credential = db.execute(
                "SELECT notification_suppressed_until FROM credentials WHERE credential_id = ?",
                (context.credential_id,),
            ).fetchone()
            suppressed = (
                str(credential["notification_suppressed_until"] or "") if credential else ""
            )
            if suppressed and suppressed > now:
                return None
            user = db.execute(
                "SELECT last_origin_context FROM users WHERE qq_id = ?",
                (context.qq_id,),
            ).fetchone()
            origin = str(user["last_origin_context"] or "") if user else ""
            db.execute(
                "UPDATE credentials SET notification_suppressed_until = ?, updated_at = ? "
                "WHERE credential_id = ?",
                (_iso(current + timedelta(hours=24)), now, context.credential_id),
            )
            return origin or None

        return await self.database.write(operation)

    async def _mark_success(self, context: _SyncContext) -> None:
        now = _iso()
        await self.database.write(
            lambda db: db.execute(
                "INSERT INTO refresh_states (refresh_kind, region_id, uid, last_attempt_at, "
                "last_success_at, failure_count, updated_at) VALUES ('role', ?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(refresh_kind, region_id, uid) DO UPDATE SET "
                "last_attempt_at = excluded.last_attempt_at, "
                "last_success_at = excluded.last_success_at, failure_count = 0, "
                "backoff_until = NULL, last_error_category = NULL, "
                "updated_at = excluded.updated_at",
                (context.region_id, context.uid, now, now, now),
            )
        )

    async def _check_user_cooldown(self, context: _SyncContext) -> None:
        if self.settings.role_refresh_cooldown_minutes == 0:
            return
        row = await self.database.read(
            lambda db: db.execute(
                "SELECT last_user_refresh_at FROM refresh_states WHERE refresh_kind = 'role' "
                "AND region_id = ? AND uid = ?",
                (context.region_id, context.uid),
            ).fetchone()
        )
        if row is None or not row["last_user_refresh_at"]:
            return
        try:
            refreshed = datetime.fromisoformat(str(row["last_user_refresh_at"]))
        except ValueError:
            return
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=UTC)
        remaining = (
            timedelta(minutes=self.settings.role_refresh_cooldown_minutes)
            - (_now() - refreshed.astimezone(UTC))
        ).total_seconds()
        if remaining > 0:
            minutes = max(1, int((remaining + 59) // 60))
            raise SyncCooldownError(f"该账号角色数据仍在刷新冷却中，请约 {minutes} 分钟后重试")

    async def _mark_user_success(self, context: _SyncContext) -> None:
        now = _iso()
        await self.database.write(
            lambda db: db.execute(
                "UPDATE refresh_states SET last_user_refresh_at = ?, updated_at = ? "
                "WHERE refresh_kind = 'role' AND region_id = ? AND uid = ?",
                (now, now, context.region_id, context.uid),
            )
        )

    async def _commit(
        self,
        context: _SyncContext,
        avatars: tuple[GuideAvatar, ...],
        characters: tuple[SyncedCharacter, ...],
    ) -> SyncResult:
        synced_at = _iso()
        owned = {character.role_id: character for character in characters}

        def operation(db: sqlite3.Connection) -> SyncResult:
            profile = db.execute(
                "SELECT profile_id FROM profiles WHERE qq_id = ? AND region_id = ? AND uid = ?",
                (context.qq_id, context.region_id, context.uid),
            ).fetchone()
            if profile is None:
                raise SyncError("UID 档案已被解绑")
            profile_id = int(profile["profile_id"])
            for avatar in avatars:
                existing = db.execute(
                    "SELECT * FROM characters WHERE profile_id = ? AND character_id = ?",
                    (profile_id, avatar.role_id),
                ).fetchone()
                if not avatar.is_acquired:
                    self._apply_unowned(db, profile_id, avatar, existing, synced_at)
                    continue
                character = owned.get(avatar.role_id)
                if character is None:
                    raise SyncError("同步快照缺少已拥有角色详情")
                self._apply_owned(db, profile_id, character, existing, synced_at)
            db.execute(
                "UPDATE game_accounts SET sync_status = 'success', last_sync_attempt_at = ?, "
                "last_sync_success_at = ?, last_error_category = NULL "
                "WHERE region_id = ? AND uid = ? AND qq_id = ?",
                (
                    synced_at,
                    synced_at,
                    context.region_id,
                    context.uid,
                    context.qq_id,
                ),
            )
            db.execute(
                "UPDATE credentials SET guide_token_status = 'valid', last_success_at = ?, "
                "updated_at = ? WHERE credential_id = ?",
                (synced_at, synced_at, context.credential_id),
            )
            return SyncResult(context.uid, context.region_id, len(characters), synced_at)

        return await self.database.write(operation)

    @staticmethod
    def _apply_unowned(
        db: sqlite3.Connection,
        profile_id: int,
        avatar: GuideAvatar,
        existing: sqlite3.Row | None,
        synced_at: str,
    ) -> None:
        if existing is None:
            return
        if str(existing["record_origin"]) == "api":
            db.execute(
                "DELETE FROM characters WHERE profile_id = ? AND character_id = ?",
                (profile_id, avatar.role_id),
            )
            return
        db.execute(
            "UPDATE characters SET api_owned = 0, api_level = NULL, api_chain = NULL, "
            "api_weapon_id = NULL, api_weapon_present = NULL, api_weapon_name = NULL, "
            "api_weapon_picture_url = NULL, api_weapon_star = NULL, api_weapon_type_id = NULL, "
            "api_weapon_type_picture_url = NULL, api_source_order = ?, "
            "record_origin = 'manual', last_api_sync_at = ?, updated_at = ? "
            "WHERE profile_id = ? AND character_id = ?",
            (avatar.source_order, synced_at, synced_at, profile_id, avatar.role_id),
        )

    @staticmethod
    def _apply_owned(
        db: sqlite3.Connection,
        profile_id: int,
        character: SyncedCharacter,
        existing: sqlite3.Row | None,
        synced_at: str,
    ) -> None:
        old_weapon = str(existing["api_weapon_id"] or "") if existing is not None else ""
        clear_weapon_levels = character.weapon_present is False or (
            character.weapon_present is True
            and old_weapon
            and old_weapon != str(character.weapon_id or "")
        )
        db.execute(
            "INSERT INTO characters (profile_id, character_id, character_name_snapshot, "
            "record_origin, api_owned, api_chain, api_weapon_id, api_weapon_present, "
            "api_weapon_name, api_weapon_picture_url, api_weapon_star, api_weapon_type_id, "
            "api_weapon_type_picture_url, "
            "api_source_order, last_api_sync_at, updated_at) "
            "VALUES (?, ?, ?, 'api', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(profile_id, character_id) DO UPDATE SET "
            "character_name_snapshot = excluded.character_name_snapshot, api_owned = 1, "
            "api_chain = COALESCE(excluded.api_chain, characters.api_chain), "
            "api_source_order = excluded.api_source_order, "
            "record_origin = CASE WHEN characters.record_origin = 'manual' THEN 'mixed' "
            "ELSE characters.record_origin END, "
            "api_weapon_id = CASE WHEN excluded.api_weapon_present IS NULL "
            "THEN characters.api_weapon_id ELSE excluded.api_weapon_id END, "
            "api_weapon_present = COALESCE(excluded.api_weapon_present, "
            "characters.api_weapon_present), "
            "api_weapon_name = CASE WHEN excluded.api_weapon_present IS NULL "
            "THEN characters.api_weapon_name ELSE excluded.api_weapon_name END, "
            "api_weapon_picture_url = CASE WHEN excluded.api_weapon_present IS NULL "
            "THEN characters.api_weapon_picture_url ELSE excluded.api_weapon_picture_url END, "
            "api_weapon_star = CASE WHEN excluded.api_weapon_present IS NULL "
            "THEN characters.api_weapon_star ELSE excluded.api_weapon_star END, "
            "api_weapon_type_id = CASE WHEN excluded.api_weapon_present IS NULL "
            "THEN characters.api_weapon_type_id ELSE excluded.api_weapon_type_id END, "
            "api_weapon_type_picture_url = CASE WHEN excluded.api_weapon_present IS NULL "
            "THEN characters.api_weapon_type_picture_url "
            "ELSE excluded.api_weapon_type_picture_url END, "
            "last_api_sync_at = excluded.last_api_sync_at, "
            "updated_at = excluded.updated_at",
            (
                profile_id,
                character.role_id,
                character.role_name,
                character.chain,
                character.weapon_id,
                None if character.weapon_present is None else int(character.weapon_present),
                character.weapon_name,
                character.weapon_picture_url,
                character.weapon_star,
                character.weapon_type_id,
                character.weapon_type_picture_url,
                character.source_order,
                synced_at,
                synced_at,
            ),
        )
        if clear_weapon_levels:
            db.execute(
                "UPDATE characters SET manual_weapon_level = NULL, "
                "manual_weapon_refinement = NULL WHERE profile_id = ? AND character_id = ?",
                (profile_id, character.role_id),
            )

    async def _auto_loop(self) -> None:
        await asyncio.sleep(random.uniform(30, 300))
        while True:
            due = await self._due_accounts()
            if due:
                await asyncio.gather(
                    *(
                        self.sync(qq_id, uid, region_id, background=True)
                        for qq_id, region_id, uid in due
                    ),
                    return_exceptions=True,
                )
            await asyncio.sleep(900 + random.uniform(0, 300))

    async def _due_accounts(self) -> tuple[tuple[str, str, str], ...]:
        threshold = _iso(_now() - timedelta(minutes=self.settings.auto_sync_interval_minutes))
        now = _iso()
        return await self.database.read(
            lambda db: tuple(
                (
                    str(row["qq_id"]),
                    str(row["region_id"]),
                    str(row["uid"]),
                )
                for row in db.execute(
                    "SELECT g.qq_id, g.region_id, g.uid FROM game_accounts g "
                    "JOIN credentials c ON c.credential_id = g.credential_id "
                    "LEFT JOIN refresh_states r ON r.refresh_kind = 'role' "
                    "AND r.region_id = g.region_id AND r.uid = g.uid "
                    "WHERE c.guide_token_status IN ('unknown', 'valid') "
                    "AND c.revoked_at IS NULL "
                    "AND (r.backoff_until IS NULL OR r.backoff_until <= ?) "
                    "AND (g.last_sync_success_at IS NULL OR g.last_sync_success_at < ?) "
                    "ORDER BY COALESCE(g.last_sync_success_at, '')",
                    (now, threshold),
                ).fetchall()
            )
        )
