"""为 Dashboard 生成不依赖真实账号的卡片样例。"""

from __future__ import annotations

from ...domain.cards import (
    AccountInfoCard,
    CardCharacter,
    CharacterDetailCard,
    CharacterListCard,
    DailyCard,
    ExplorationCard,
    HelpCard,
    HelpSection,
    PlayerHeader,
)
from ..resources import UiAssetManifest
from .renderer import AstrBotCardRenderer

_KINDS = {
    "character_list",
    "character_detail",
    "account_info",
    "daily",
    "exploration",
    "help",
}


class CardPreviewService:
    def __init__(self, renderer: AstrBotCardRenderer, manifest: UiAssetManifest):
        self.renderer = renderer
        self.manifest = manifest

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(_KINDS))

    async def render(self, kind: str):
        if kind not in _KINDS:
            raise ValueError("不支持的卡片预览类型")
        return await self.renderer.render(self._model(kind))

    def _model(self, kind: str):
        characters = tuple(
            self._character(item, index) for index, item in enumerate(self.manifest.characters[:15])
        )
        player = PlayerHeader(
            avatar_id=characters[0].character_id if characters else None,
            image_url=characters[0].image_url if characters else None,
            name="漂泊者",
            uid="700012345",
            region_name="SEA",
            level=80,
            world_level=8,
            role_count=57,
        )
        if kind == "character_list":
            return CharacterListCard(
                kind=kind,
                scope="dashboard-preview-character-list",
                heading="角色总览",
                profile_note=None,
                player=player,
                total_characters=len(characters),
                characters=characters,
                updated_at="预览",
            )
        if kind == "character_detail":
            return CharacterDetailCard(
                kind=kind,
                scope="dashboard-preview-character-detail",
                heading="角色档案",
                profile_note=None,
                character=characters[0],
            )
        if kind == "account_info":
            return AccountInfoCard(
                kind=kind,
                scope="dashboard-preview-account",
                heading="账号信息",
                profile_note=None,
                player=player,
                active_days=412,
                created_at="2024-07-01 09:12 UTC",
                refreshed_at="预览",
            )
        if kind == "daily":
            return DailyCard(
                kind=kind,
                scope="dashboard-preview-daily",
                heading="日常状态",
                profile_note=None,
                player=player,
                energy=186,
                max_energy=240,
                energy_recover_at="约 4小时30分钟后",
                store_energy=120,
                max_store_energy=480,
                store_energy_recover_at="约 2天后",
                liveness=80,
                liveness_max=100,
                liveness_unlock=True,
                weekly_inst_count=2,
                battle_pass_present=True,
                battle_pass_level=43,
                battle_pass_week_exp=6200,
                battle_pass_week_max_exp=10000,
                battle_pass_is_unlock=True,
                battle_pass_is_open=True,
                battle_pass_exp=640,
                battle_pass_exp_limit=1000,
                refreshed_at="预览",
            )
        if kind == "exploration":
            return ExplorationCard(
                kind=kind,
                scope="dashboard-preview-exploration",
                heading="探索收集",
                profile_note=None,
                player=player,
                sound_box=126,
                boxes=(
                    ("朴素奇藏箱", 332),
                    ("基准奇藏箱", 215),
                    ("精密奇藏箱", 96),
                    ("辉光奇藏箱", 31),
                ),
                basic_boxes=(
                    ("朴素奇藏箱", 228),
                    ("基准奇藏箱", 152),
                    ("精密奇藏箱", 64),
                    ("辉光奇藏箱", 18),
                ),
                phantom_boxes=(("绿色潮汐之遗", 15), ("紫色潮汐之遗", 8), ("金色潮汐之遗", 3)),
                refreshed_at="预览",
            )
        return HelpCard(
            kind=kind,
            scope="dashboard-preview-help",
            heading="鸣潮国际服数据工具",
            subtitle="命令入口 /kh · 同时兼容已配置关键词与 @Bot 场景",
            sections=(
                HelpSection(
                    "查询", (("/kh 角色 [@用户]", "角色总览长图"), ("/kh 日常", "波片、周常与电台"))
                ),
                HelpSection(
                    "账号", (("/kh 登录", "创建限时登录链接"), ("/kh 刷新 [UID]", "主动刷新数据"))
                ),
                HelpSection(
                    "手动维护",
                    (
                        ("/kh 修改 <角色> 等级 <1-90>", "覆盖角色等级"),
                        ("/kh 重置 <角色> 全部", "移除手动覆盖"),
                    ),
                ),
                HelpSection(
                    "确认", (("/kh 确认", "确认待执行操作"), ("/kh 取消", "取消待执行操作"))
                ),
            ),
            updated_at=None,
        )

    @staticmethod
    def _character(item: dict[str, object], index: int) -> CardCharacter:
        names = item.get("names") if isinstance(item.get("names"), dict) else {}
        return CardCharacter(
            character_id=str(item.get("id") or index),
            name=str(names.get("zh-CN") or names.get("en") or "角色"),
            image_url=str(item.get("card_picture_url") or "") or None,
            illustration_picture_url=str(item.get("illustration_picture_url") or "") or None,
            star=int(item.get("star") or 5),
            element_id=str(item.get("element_id") or "") or None,
            element_name="属性",
            element_image_url=None,
            origin="api",
            level=90 - index % 10,
            level_source="api",
            chain=index % 7,
            chain_source="api",
            weapon_id=None,
            weapon_name="示例武器" if index == 0 else None,
            weapon_image_url=None,
            weapon_star=5 if index == 0 else None,
            weapon_type_id="2102" if index == 0 else None,
            weapon_type_name="迅刀" if index == 0 else None,
            weapon_type_image_url=None,
            weapon_source="api" if index == 0 else None,
            weapon_level=90 if index == 0 else None,
            weapon_refinement=1 if index == 0 else None,
            score_total=None,
            score_grade=None,
            updated_at="预览",
        )
