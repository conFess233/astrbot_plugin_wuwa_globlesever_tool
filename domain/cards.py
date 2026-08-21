"""查询图片卡使用的稳定 ViewModel 与消息结果。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PlayerHeader:
    image_url: str | None
    name: str
    uid: str
    region_name: str
    level: int | None
    world_level: int | None
    role_count: int | None


@dataclass(frozen=True, slots=True)
class CardCharacter:
    character_id: str
    name: str
    image_url: str | None
    illustration_picture_url: str | None
    star: int | None
    element_name: str
    element_image_url: str | None
    origin: str
    level: int | None
    level_source: str | None
    chain: int | None
    chain_source: str | None
    weapon_id: str | None
    weapon_name: str | None
    weapon_image_url: str | None
    weapon_star: int | None
    weapon_type_name: str | None
    weapon_type_image_url: str | None
    weapon_source: str | None
    weapon_level: int | None
    weapon_refinement: int | None
    score: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CharacterListCard:
    kind: str
    scope: str
    heading: str
    profile_note: str | None
    player: PlayerHeader | None
    total_characters: int
    characters: tuple[CardCharacter, ...]
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class CharacterDetailCard:
    kind: str
    scope: str
    heading: str
    profile_note: str | None
    character: CardCharacter


@dataclass(frozen=True, slots=True)
class AccountInfoCard:
    kind: str
    scope: str
    heading: str
    profile_note: str | None
    player: PlayerHeader
    active_days: int | None
    created_at: str
    refreshed_at: str


@dataclass(frozen=True, slots=True)
class DailyCard:
    kind: str
    scope: str
    heading: str
    profile_note: str | None
    player: PlayerHeader
    energy: int | None
    max_energy: int | None
    energy_recover_at: str
    store_energy: int | None
    max_store_energy: int | None
    store_energy_recover_at: str
    liveness: int | None
    liveness_max: int | None
    liveness_unlock: bool | None
    weekly_inst_count: int | None
    refreshed_at: str


@dataclass(frozen=True, slots=True)
class ExplorationCard:
    kind: str
    scope: str
    heading: str
    profile_note: str | None
    player: PlayerHeader
    sound_box: int | None
    boxes: tuple[tuple[str, int | None], ...]
    basic_boxes: tuple[tuple[str, int | None], ...]
    phantom_boxes: tuple[tuple[str, int | None], ...]
    refreshed_at: str


CardViewModel = (
    CharacterListCard | CharacterDetailCard | AccountInfoCard | DailyCard | ExplorationCard
)


@dataclass(frozen=True, slots=True)
class CardMessage:
    image_path: Path
    fallback_text: str
