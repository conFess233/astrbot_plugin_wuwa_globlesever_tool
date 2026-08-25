"""从查询记录构建统一 ViewModel，并通过 AstrBot HTML 渲染图片卡。"""

import logging
from datetime import UTC, datetime

from ..domain.cards import (
    AccountInfoCard,
    CardCharacter,
    CardMessage,
    CardViewModel,
    CharacterDetailCard,
    CharacterListCard,
    DailyCard,
    ExplorationCard,
    HelpCard,
    HelpSection,
    PlayerHeader,
)
from ..domain.catalog import CharacterCatalog
from ..domain.player import PlayerSnapshot
from ..infrastructure.database.repositories.local_data import CharacterRecord, ProfileSelection
from ..presentation.cards import AstrBotCardRenderer

logger = logging.getLogger(__name__)

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

    async def help(self) -> str | CardMessage:
        model = HelpCard(
            kind="help",
            scope="global-help",
            heading="鸣潮国际服数据工具",
            subtitle="命令入口 /kh · 同时兼容已配置关键词与 @Bot 场景",
            sections=(
                HelpSection(
                    "查询",
                    (
                        ("/kh 角色 [@用户]", "角色总览长图"),
                        ("/kh 角色 <角色> [@用户]", "角色详细档案"),
                        ("/kh 账号信息 [@用户]", "账号基础信息"),
                        ("/kh 日常", "结晶波片、活跃度、周常与电台"),
                        ("/kh 探索 [@用户]", "收集与奇藏箱数据"),
                    ),
                ),
                HelpSection(
                    "账号",
                    (
                        ("/kh 登录", "创建限时网页登录链接"),
                        ("/kh 取消登录", "立即作废当前登录链接"),
                        ("/kh 账号", "查看已绑定区服账号"),
                        ("/kh 切换 <编号|UID|本地>", "切换活动档案"),
                        ("/kh 刷新 [UID]", "主动刷新角色与账号数据"),
                    ),
                ),
                HelpSection(
                    "手动维护",
                    (
                        ("/kh 修改 <角色> 等级 <1-90>", "覆盖角色等级"),
                        ("/kh 修改 <角色> 共鸣链 <0-6>", "覆盖共鸣链"),
                        ("/kh 修改 <角色> 武器 <名称>", "记录武器名称"),
                        ("/kh 修改 <角色> 武器等级 <1-90>", "记录武器等级"),
                        ("/kh 修改 <角色> 武器精炼 <1-5>", "记录武器精炼"),
                        ("/kh 重置 <角色> <字段|全部>", "移除手动覆盖"),
                        ("/kh 删除角色 <角色>", "删除纯本地角色"),
                    ),
                ),
                HelpSection(
                    "确认",
                    (
                        ("/kh 确认", "确认当前会话中的待执行操作"),
                        ("/kh 取消", "取消当前待执行操作"),
                        ("/kh 解绑 <UID>", "发起账号解绑"),
                    ),
                ),
            ),
            updated_at=None,
        )
        return await self._response(model, _help_text(model))

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
            profile_note=None,
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
            profile_note=None,
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
            battle_pass_present=snapshot.battle_pass_present,
            battle_pass_level=snapshot.battle_pass_level,
            battle_pass_week_exp=snapshot.battle_pass_week_exp,
            battle_pass_week_max_exp=snapshot.battle_pass_week_max_exp,
            battle_pass_is_unlock=snapshot.battle_pass_is_unlock,
            battle_pass_is_open=snapshot.battle_pass_is_open,
            battle_pass_exp=snapshot.battle_pass_exp,
            battle_pass_exp_limit=snapshot.battle_pass_exp_limit,
            refreshed_at=_display_time(snapshot.refreshed_at),
        )
        return await self._response(model, _daily_text(model))

    async def exploration(self, snapshot: PlayerSnapshot) -> str | CardMessage:
        model = ExplorationCard(
            kind="exploration",
            scope=f"uid-{snapshot.uid}-exploration",
            heading="探索收集",
            profile_note=None,
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
            avatar_id=str(snapshot.head_photo) if snapshot.head_photo is not None else None,
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
            element_id=definition.element_id,
            element_name=_ELEMENT_NAMES.get(definition.element_id or "", "未知属性"),
            element_image_url=definition.element_picture_url,
            origin=record.record_origin,
            level=record.level,
            level_source=record.level_source,
            chain=record.chain,
            chain_source=record.chain_source,
            weapon_id=record.weapon_id,
            weapon_name=record.weapon_name,
            weapon_image_url=record.weapon_picture_url,
            weapon_star=record.weapon_star,
            weapon_type_id=record.weapon_type_id,
            weapon_type_name=_weapon_type_name(record),
            weapon_type_image_url=record.weapon_type_picture_url,
            weapon_source=record.weapon_source,
            weapon_level=record.weapon_level,
            weapon_refinement=record.weapon_refinement,
            score_total=record.score_total,
            score_grade=record.score_grade,
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
        f"{item.name}  Lv.{_value(item.level)}  {_value(item.chain)}链" for item in model.characters
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
    ]
    if not model.battle_pass_present:
        lines.append("先约电台：—")
    else:
        status = (
            "未解锁"
            if model.battle_pass_is_unlock is False
            else "未开启"
            if model.battle_pass_is_open is False
            else "已开启"
            if model.battle_pass_is_open is True
            else "—"
        )
        lines.extend(
            [
                f"先约电台：{status} · 等级 {_value(model.battle_pass_level)}",
                "电台经验："
                f"{_ratio(model.battle_pass_exp, model.battle_pass_exp_limit)} · "
                f"本周 {_ratio(model.battle_pass_week_exp, model.battle_pass_week_max_exp)}",
            ]
        )
    lines.append(f"更新时间：{model.refreshed_at}")
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


def _help_text(model: HelpCard) -> str:
    lines = [model.heading, model.subtitle]
    for section in model.sections:
        lines.append(f"\n{section.title}")
        lines.extend(f"{command} · {description}" for command, description in section.commands)
    return "\n".join(lines)


def _collection_rows(
    values: tuple[tuple[str, int], ...] | None,
    labels: tuple[str, ...],
) -> tuple[tuple[str, int | None], ...]:
    mapping = dict(values or ())
    return tuple((label, mapping.get(str(index))) for index, label in enumerate(labels, 1))


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


def _weapon_type_name(record: CharacterRecord) -> str | None:
    weapon_id = str(record.weapon_id or "")
    if len(weapon_id) >= 4:
        name = _WEAPON_PREFIX_NAMES.get(weapon_id[:4])
        if name is not None:
            return name
    return None
