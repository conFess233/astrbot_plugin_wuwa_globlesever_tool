"""从查询记录构建统一 ViewModel，并通过 AstrBot HTML 渲染图片卡。"""

import asyncio
import hashlib
import json
import logging
import shutil
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path

from domain.cards import (
    CardCharacter,
    CardMessage,
    CardViewModel,
    CharacterDetailCard,
    CharacterListCard,
    ProgressCard,
)
from repositories.local_data import CharacterRecord, ProfileSelection
from services.catalog import CharacterCatalog
from services.resource_cache import StaticImageCache

logger = logging.getLogger(__name__)

_RENDER_SCHEMA = "wuwa-card-v1"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_CARD_BYTES = 16 * 1024 * 1024
_SOURCE_NAMES = {"api": "接口", "manual": "手动", "mixed": "混合"}
_ORIGIN_NAMES = {"api": "接口", "manual": "手动", "mixed": "混合"}

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
    ):
        self.template_path = template_path
        self.cache_directory = cache_directory
        self.html_render = html_render
        self.timeout_seconds = timeout_seconds
        self.resource_cache = resource_cache
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

        async def resolve(url: str) -> str | None:
            async with semaphore:
                return await self.resource_cache.data_uri(url)

        resolved = await asyncio.gather(*(resolve(url) for _, _, url in locations))
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
        page: int,
        total_pages: int,
        total_characters: int,
        records: list[CharacterRecord],
    ) -> str | CardMessage:
        model = CharacterListCard(
            kind="character_list",
            scope=f"profile-{profile.profile_id}-characters-page-{page}",
            heading=heading,
            profile_note=_profile_note(profile),
            page=page,
            total_pages=total_pages,
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

    async def progress(
        self,
        profile: ProfileSelection,
        heading: str,
        records: list[CharacterRecord],
    ) -> str | CardMessage:
        levels = [item.level for item in records if item.level is not None]
        chains = [item.chain for item in records if item.chain is not None]
        known_core = sum(
            value is not None
            for item in records
            for value in (item.level, item.chain, item.weapon_id)
        )
        core_total = len(records) * 3
        buckets = (
            ("1-40", sum(level <= 40 for level in levels)),
            ("41-60", sum(41 <= level <= 60 for level in levels)),
            ("61-79", sum(61 <= level <= 79 for level in levels)),
            ("80-90", sum(level >= 80 for level in levels)),
            ("未知", len(records) - len(levels)),
        )
        origins = Counter(item.record_origin for item in records)
        model = ProgressCard(
            kind="progress",
            scope=f"profile-{profile.profile_id}-progress",
            heading=heading,
            profile_note=_profile_note(profile),
            total_characters=len(records),
            average_level=sum(levels) / len(levels) if levels else None,
            total_chains=sum(chains) if chains else None,
            high_level_count=sum(level >= 80 for level in levels),
            high_chain_count=sum(chain >= 3 for chain in chains),
            completeness_percent=round(known_core * 100 / core_total) if core_total else 0,
            level_buckets=buckets,
            origin_counts=tuple(
                (_ORIGIN_NAMES[key], origins.get(key, 0)) for key in ("api", "manual", "mixed")
            ),
            score="---",
            updated_at=_latest_update(records),
        )
        return await self._response(model, _progress_text(model))

    def _character(self, record: CharacterRecord) -> CardCharacter:
        definition = self.catalog.resolve(record.character_id)
        return CardCharacter(
            character_id=record.character_id,
            name=record.character_name,
            image_url=definition.card_picture_url,
            star=definition.star,
            element_id=definition.element_id,
            element_image_url=definition.element_picture_url,
            origin=record.record_origin,
            level=record.level,
            level_source=record.level_source,
            chain=record.chain,
            chain_source=record.chain_source,
            weapon_id=record.weapon_id,
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
    lines = [f"{model.heading}（{model.page}/{model.total_pages}）"]
    if model.profile_note:
        lines.append(model.profile_note)
    if not model.characters:
        lines.append("暂无角色记录，可用 /kh 修改 <角色> 等级 <1-90> 创建。")
    else:
        lines.extend(
            f"{item.name}  Lv.{_value(item.level)}  {_value(item.chain)}链  "
            f"武器 {item.weapon_id or '---'}  来源 {_source(item.origin)}  "
            f"评分 {item.score}  更新 {_display_time(item.updated_at)}"
            for item in model.characters
        )
    return "\n".join(lines)


def _detail_text(model: CharacterDetailCard) -> str:
    item = model.character
    lines = [model.heading]
    if model.profile_note:
        lines.append(model.profile_note)
    lines.extend(
        (
            f"角色 ID：{item.character_id}",
            f"星级：{_value(item.star)}",
            f"属性 ID：{item.element_id or '---'}",
            "武器类型：---",
            f"等级：{_value(item.level)}（{_source(item.level_source)}）",
            f"共鸣链：{_value(item.chain)}（{_source(item.chain_source)}）",
            f"武器：{item.weapon_id or '---'}（{_source(item.weapon_source)}）",
            "武器星级：---",
            f"武器等级：{_value(item.weapon_level)}",
            f"武器精炼：{_value(item.weapon_refinement)}",
            f"记录来源：{_source(item.origin)}",
            f"最后更新：{_display_time(item.updated_at)}",
            f"评分：{item.score}",
        )
    )
    return "\n".join(lines)


def _progress_text(model: ProgressCard) -> str:
    lines = [model.heading]
    if model.profile_note:
        lines.append(model.profile_note)
    if not model.total_characters:
        lines.append("暂无角色记录。")
        return "\n".join(lines)
    lines.extend(
        (
            f"角色数：{model.total_characters}",
            f"平均等级：{model.average_level:.1f}"
            if model.average_level is not None
            else "平均等级：---",
            f"共鸣链总数：{model.total_chains}"
            if model.total_chains is not None
            else "共鸣链总数：---",
            f"高等级角色（≥80）：{model.high_level_count}",
            f"高共鸣链角色（≥3）：{model.high_chain_count}",
            f"核心资料完整率：{model.completeness_percent}%",
            "等级分布：" + "、".join(f"{name} {count}" for name, count in model.level_buckets),
            "数据来源：" + "、".join(f"{name} {count}" for name, count in model.origin_counts),
            f"最后更新：{_display_time(model.updated_at)}",
            f"评分：{model.score}",
        )
    )
    return "\n".join(lines)


def _profile_note(profile: ProfileSelection) -> str | None:
    return "纯本地数据，未经接口验证" if profile.profile_type == "local" else None


def _latest_update(records: list[CharacterRecord]) -> str | None:
    return max((item.updated_at for item in records), default=None)


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
