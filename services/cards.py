"""从查询记录构建统一 ViewModel，并通过 AstrBot HTML 渲染图片卡。"""

import asyncio
import hashlib
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from ..domain.cards import (
    AccountInfoCard,
    CardCharacter,
    CardMessage,
    CardViewModel,
    CharacterDetailCard,
    CharacterListCard,
    DailyCard,
    ExplorationCard,
    PlayerHeader,
)
from ..domain.player import PlayerSnapshot
from ..repositories.local_data import CharacterRecord, ProfileSelection
from .catalog import CharacterCatalog
from .resource_cache import StaticImageCache

logger = logging.getLogger(__name__)

_RENDER_SCHEMA = "wuwa-card-v2"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_CARD_BYTES = 24 * 1024 * 1024
_SOURCE_NAMES = {"api": "接口", "manual": "手动", "mixed": "混合"}
_ELEMENT_NAMES = {
    "1": "冷凝",
    "2": "热熔",
    "3": "导电",
    "4": "气动",
    "5": "衍射",
    "6": "湮灭",
}
_WEAPON_PREFIX_NAMES = {
    "2101": "长刃",
    "2102": "迅刀",
    "2103": "佩枪",
    "2104": "臂铠",
    "2105": "音感仪",
}
_BOX_NAMES = ("朴素奇藏箱", "基准奇藏箱", "精密奇藏箱", "辉光奇藏箱")
_PHANTOM_NAMES = ("绿色潮汐之遗", "紫色潮汐之遗", "金色潮汐之遗")

HtmlRender = Callable[[str, dict[str, object], dict[str, object]], Awaitable[str]]


class CardRenderError(RuntimeError):
    """表示 AstrBot HTML 渲染或渲染缓存不可用。"""


class AstrBotCardRenderer:
    def __init__(
        self,
        template_path: Path,
        cache_directory: Path,
        html_render: HtmlRender,
        timeout_seconds: int,
        resource_cache: StaticImageCache | None = None,
        weapon_resource_cache: StaticImageCache | None = None,
    ):
        self.template_path = template_path
        self.cache_directory = cache_directory
        self.html_render = html_render
        self.timeout_seconds = timeout_seconds
        self.resource_cache = resource_cache
        self.weapon_resource_cache = weapon_resource_cache
        self._locks: dict[str, asyncio.Lock] = {}

    async def render(self, view_model: CardViewModel) -> Path:
        payload = asdict(view_model)
        digest = hashlib.sha256(
            json.dumps(
                {"schema": _RENDER_SCHEMA, "payload": payload},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        scope = _safe_scope(view_model.scope)
        target = self.cache_directory / f"{scope}-{digest}.png"
        if await asyncio.to_thread(_valid_png, target):
            return target

        lock = self._locks.setdefault(scope, asyncio.Lock())
        async with lock:
            if await asyncio.to_thread(_valid_png, target):
                return target
            try:
                template = await asyncio.to_thread(self.template_path.read_text, encoding="utf-8")
                rendered_path = await asyncio.wait_for(
                    self._render_uncached(template, payload),
                    timeout=self.timeout_seconds,
                )
                source = await asyncio.to_thread(_absolute_path, rendered_path)
                if not await asyncio.to_thread(_valid_png, source):
                    raise CardRenderError("AstrBot 返回的图片文件无效")
                await asyncio.to_thread(self.cache_directory.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copyfile, source, target)
                await asyncio.to_thread(self._prune_scope, scope, target)
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                raise CardRenderError("图片渲染失败") from exc
            if not await asyncio.to_thread(_valid_png, target):
                raise CardRenderError("图片缓存写入失败")
            return target

    async def _render_uncached(self, template: str, payload: dict[str, object]) -> str:
        render_payload = await self._localize_images(payload)
        return await self.html_render(
            template,
            render_payload,
            {
                "type": "png",
                "full_page": True,
                "animations": "disabled",
                "scale": "css",
            },
        )

    async def _localize_images(self, payload: dict[str, object]) -> dict[str, object]:
        if self.resource_cache is None:
            return payload
        result = dict(payload)
        locations: list[tuple[dict[str, object], str, str]] = []
        self._collect_images(result, locations)
        semaphore = asyncio.Semaphore(4)

        async def resolve(key: str, url: str) -> str | None:
            cache = (
                self.weapon_resource_cache
                if key.startswith("weapon_") and self.weapon_resource_cache is not None
                else self.resource_cache
            )
            async with semaphore:
                return await cache.data_uri(url)

        resolved = await asyncio.gather(*(resolve(key, url) for _, key, url in locations))
        for (container, key, _), data_uri in zip(locations, resolved, strict=True):
            container[key] = data_uri
        return result

    def _collect_images(
        self,
        value: object,
        locations: list[tuple[dict[str, object], str, str]],
    ) -> None:
        if isinstance(value, dict):
            for key, item in tuple(value.items()):
                if key.endswith("_url") and isinstance(item, str) and item:
                    locations.append((value, key, item))
                else:
                    self._collect_images(item, locations)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._collect_images(item, locations)

    def _prune_scope(self, scope: str, keep: Path) -> None:
        for candidate in self.cache_directory.glob(f"{scope}-*.png"):
            if candidate != keep and candidate.parent == self.cache_directory:
                candidate.unlink(missing_ok=True)


class CardService:
    def __init__(
        self,
        catalog: CharacterCatalog,
        renderer: AstrBotCardRenderer | None = None,
    ):
        self.catalog = catalog
        self.renderer = renderer

    async def character_list(
        self,
        profile: ProfileSelection,
        heading: str,
        total_characters: int,
        records: list[CharacterRecord],
        player: PlayerSnapshot | None = None,
    ) -> str | CardMessage:
        model = CharacterListCard(
            kind="character_list",
            scope=f"profile-{profile.profile_id}-characters",
            heading=heading,
            profile_note=_profile_note(profile),
            player=self._player_header(player) if player is not None else None,
            total_characters=total_characters,
            characters=tuple(self._character(record) for record in records),
            updated_at=_latest_update(records),
        )
        return await self._response(model, _list_text(model))

    async def character_detail(
        self,
        profile: ProfileSelection,
        profile_label: str,
        record: CharacterRecord,
    ) -> str | CardMessage:
        character = self._character(record)
        model = CharacterDetailCard(
            kind="character_detail",
            scope=f"profile-{profile.profile_id}-character-{record.character_id}",
            heading=f"{record.character_name} · {profile_label}档案",
            profile_note=_profile_note(profile),
            character=character,
        )
        return await self._response(model, _detail_text(model))

    async def account_info(self, snapshot: PlayerSnapshot) -> str | CardMessage:
        model = AccountInfoCard(
            kind="account_info",
            scope=f"uid-{snapshot.uid}-account-info",
            heading="账号信息",
            profile_note=_cache_note(snapshot),
            player=self._player_header(snapshot),
            active_days=snapshot.active_days,
            created_at=_timestamp(snapshot.created_at_ms),
            refreshed_at=_display_time(snapshot.refreshed_at),
        )
        return await self._response(model, _account_text(model))

    async def daily(self, snapshot: PlayerSnapshot) -> str | CardMessage:
        model = DailyCard(
            kind="daily",
            scope=f"uid-{snapshot.uid}-daily",
            heading="日常状态",
            profile_note=_cache_note(snapshot),
            player=self._player_header(snapshot),
            energy=snapshot.energy,
            max_energy=snapshot.max_energy,
            energy_recover_at=_recovery_time(snapshot.energy_recover_time_ms),
            store_energy=snapshot.store_energy,
            max_store_energy=snapshot.max_store_energy,
            store_energy_recover_at=_recovery_time(snapshot.store_energy_recover_time_ms),
            liveness=snapshot.liveness,
            liveness_max=snapshot.liveness_max,
            liveness_unlock=snapshot.liveness_unlock,
            weekly_inst_count=snapshot.weekly_inst_count,
            refreshed_at=_display_time(snapshot.refreshed_at),
        )
        return await self._response(model, _daily_text(model))

    async def exploration(self, snapshot: PlayerSnapshot) -> str | CardMessage:
        model = ExplorationCard(
            kind="exploration",
            scope=f"uid-{snapshot.uid}-exploration",
            heading="探索收集",
            profile_note=_cache_note(snapshot),
            player=self._player_header(snapshot),
            sound_box=snapshot.sound_box,
            boxes=_collection_rows(snapshot.boxes, _BOX_NAMES),
            basic_boxes=_collection_rows(snapshot.basic_boxes, _BOX_NAMES),
            phantom_boxes=_collection_rows(snapshot.phantom_boxes, _PHANTOM_NAMES),
            refreshed_at=_display_time(snapshot.refreshed_at),
        )
        return await self._response(model, _exploration_text(model))

    def _player_header(self, snapshot: PlayerSnapshot) -> PlayerHeader:
        avatar = self.catalog.get(snapshot.head_photo)
        return PlayerHeader(
            image_url=avatar.card_picture_url if avatar is not None else None,
            name=snapshot.player_name or "漂泊者",
            uid=snapshot.uid,
            region_name=snapshot.region_name,
            level=snapshot.level,
            world_level=snapshot.world_level,
            role_count=snapshot.role_num,
        )

    def _character(self, record: CharacterRecord) -> CardCharacter:
        definition = self.catalog.resolve(record.character_id)
        return CardCharacter(
            character_id=record.character_id,
            name=record.character_name,
            image_url=definition.card_picture_url,
            illustration_picture_url=definition.illustration_picture_url,
            star=definition.star,
            element_name=_ELEMENT_NAMES.get(definition.element_id or "", "未知属性"),
            element_image_url=definition.element_picture_url,
            origin=record.record_origin,
            level=record.level,
            level_source=record.level_source,
            chain=record.chain,
            chain_source=record.chain_source,
            weapon_id=record.weapon_id,
            weapon_name=record.weapon_name
            or (f"武器 {record.weapon_id}" if record.weapon_id else None),
            weapon_image_url=record.weapon_picture_url,
            weapon_star=record.weapon_star,
            weapon_type_name=_weapon_type_name(record),
            weapon_type_image_url=record.weapon_type_picture_url,
            weapon_source=record.weapon_source,
            weapon_level=record.weapon_level,
            weapon_refinement=record.weapon_refinement,
            score=_score(record),
            updated_at=record.updated_at,
        )

    async def _response(self, model: CardViewModel, fallback: str) -> str | CardMessage:
        if self.renderer is None:
            return fallback
        try:
            return CardMessage(await self.renderer.render(model), fallback)
        except Exception:  # noqa: BLE001 - 外部渲染失败必须统一降级为文本
            logger.warning("鸣潮查询图片渲染失败，已降级为文本", exc_info=True)
            return fallback


def _list_text(model: CharacterListCard) -> str:
    lines = [f"{model.heading} · 已拥有 {model.total_characters} 名角色"]
    if model.profile_note:
        lines.append(model.profile_note)
    lines.extend(
        f"{item.name}  Lv.{_value(item.level)}  {_value(item.chain)}链  评分 {item.score}"
        for item in model.characters
    )
    if not model.characters:
        lines.append("暂无角色记录，可用 /kh 修改 <角色> 等级 <1-90> 创建。")
    return "\n".join(lines)


def _detail_text(model: CharacterDetailCard) -> str:
    item = model.character
    lines = [model.heading]
    if model.profile_note:
        lines.append(model.profile_note)
    lines.extend(
        (
            f"属性：{item.element_name}",
            f"等级：{_value(item.level)}（{_source(item.level_source)}）",
            f"共鸣链：{_value(item.chain)}（{_source(item.chain_source)}）",
            f"武器：{item.weapon_name or '---'}（{_source(item.weapon_source)}）",
            f"武器星级：{_value(item.weapon_star)}",
            f"武器类型：{item.weapon_type_name or '---'}",
            f"武器等级：{_value(item.weapon_level)}",
            f"武器精炼：{_value(item.weapon_refinement)}",
            f"最后更新：{_display_time(item.updated_at)}",
            f"评分：{item.score}",
        )
    )
    return "\n".join(lines)


def _account_text(model: AccountInfoCard) -> str:
    player = model.player
    lines = [
        model.heading,
        f"{player.name} · UID {player.uid} · {player.region_name}",
        f"账号等级：{_value(player.level)} · 索拉等级：{_value(player.world_level)}",
        f"已拥有角色：{_value(player.role_count)}",
        f"活跃天数：{_value(model.active_days)}",
        f"创建时间：{model.created_at}",
        f"更新时间：{model.refreshed_at}",
    ]
    if model.profile_note:
        lines.insert(1, model.profile_note)
    return "\n".join(lines)


def _daily_text(model: DailyCard) -> str:
    lines = [
        f"{model.player.name} · 日常状态",
        f"结晶波片：{_ratio(model.energy, model.max_energy)} · 回满 {model.energy_recover_at}",
        f"储备结晶单质：{_ratio(model.store_energy, model.max_store_energy)}"
        f" · 回满 {model.store_energy_recover_at}",
        "每日活跃度：未解锁"
        if model.liveness_unlock is False
        else f"每日活跃度：{_ratio(model.liveness, model.liveness_max)}",
        f"战歌重奏剩余奖励：{_value(model.weekly_inst_count)} / 3",
        f"更新时间：{model.refreshed_at}",
    ]
    if model.profile_note:
        lines.insert(1, model.profile_note)
    return "\n".join(lines)


def _exploration_text(model: ExplorationCard) -> str:
    lines = [f"{model.player.name} · 探索收集", f"声匣：{_value(model.sound_box)}"]
    lines.append("奇藏箱：" + "、".join(f"{name} {_value(value)}" for name, value in model.boxes))
    lines.append(
        "基础箱统计：" + "、".join(f"{name} {_value(value)}" for name, value in model.basic_boxes)
    )
    lines.append(
        "潮汐之遗：" + "、".join(f"{name} {_value(value)}" for name, value in model.phantom_boxes)
    )
    lines.append(f"更新时间：{model.refreshed_at}")
    if model.profile_note:
        lines.insert(1, model.profile_note)
    return "\n".join(lines)


def _collection_rows(
    values: tuple[tuple[str, int], ...] | None,
    labels: tuple[str, ...],
) -> tuple[tuple[str, int | None], ...]:
    mapping = dict(values or ())
    return tuple((label, mapping.get(str(index))) for index, label in enumerate(labels, 1))


def _cache_note(snapshot: PlayerSnapshot) -> str | None:
    return (
        f"实时刷新失败，当前展示 {_display_time(snapshot.refreshed_at)} 的本地缓存"
        if snapshot.is_cached_fallback
        else None
    )


def _profile_note(profile: ProfileSelection) -> str | None:
    return "纯本地数据，未经接口验证" if profile.profile_type == "local" else None


def _latest_update(records: list[CharacterRecord]) -> str | None:
    return max((item.updated_at for item in records), default=None)


def _timestamp(value: int | None) -> str:
    if not value:
        return "---"
    try:
        timestamp = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return "---"


def _recovery_time(value: int | None) -> str:
    if not value:
        return "---"
    try:
        target = datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return "---"
    seconds = (target - datetime.now(UTC)).total_seconds()
    if seconds <= 0:
        return "已回满"
    total_minutes = max(1, int((seconds + 59) // 60))
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f"约 {days}天{hours}小时后"
    if hours:
        return f"约 {hours}小时{minutes}分钟后"
    return f"约 {minutes}分钟后"


def _ratio(value: int | None, maximum: int | None) -> str:
    return f"{_value(value)} / {_value(maximum)}"


def _value(value: object | None) -> str:
    return "---" if value is None else str(value)


def _source(value: str | None) -> str:
    return _SOURCE_NAMES.get(value or "", "---")


def _display_time(value: str | None) -> str:
    if not value:
        return "---"
    return value.replace("T", " ").replace("+00:00", " UTC")


def _score(record: CharacterRecord) -> str:
    if record.score_total is None:
        return "---"
    grade = f" {record.score_grade}" if record.score_grade else ""
    return f"{record.score_total:g}{grade}"


def _weapon_type_name(record: CharacterRecord) -> str | None:
    weapon_id = str(record.weapon_id or "")
    if len(weapon_id) >= 4:
        name = _WEAPON_PREFIX_NAMES.get(weapon_id[:4])
        if name is not None:
            return name
    if record.weapon_type_id:
        return f"类型 {record.weapon_type_id}"
    return None


def _safe_scope(value: str) -> str:
    return "".join(
        character if character.isalnum() or character == "-" else "-" for character in value
    )


def _valid_png(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if not 8 < size <= _MAX_CARD_BYTES:
            return False
        with path.open("rb") as stream:
            return stream.read(8) == _PNG_SIGNATURE
    except OSError:
        return False


def _absolute_path(value: str) -> Path:
    return Path(value).resolve()
