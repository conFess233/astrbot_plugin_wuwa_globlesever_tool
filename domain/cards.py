"""查询图片卡使用的稳定 ViewModel 与消息结果。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CardCharacter:
    character_id: str
    name: str
    image_url: str | None
    star: int | None
    element_id: str | None
    element_image_url: str | None
    origin: str
    level: int | None
    level_source: str | None
    chain: int | None
    chain_source: str | None
    weapon_id: str | None
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
    page: int
    total_pages: int
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
class ProgressCard:
    kind: str
    scope: str
    heading: str
    profile_note: str | None
    total_characters: int
    average_level: float | None
    total_chains: int | None
    high_level_count: int
    high_chain_count: int
    completeness_percent: int
    level_buckets: tuple[tuple[str, int], ...]
    origin_counts: tuple[tuple[str, int], ...]
    score: str
    updated_at: str | None


CardViewModel = CharacterListCard | CharacterDetailCard | ProgressCard


@dataclass(frozen=True, slots=True)
class CardMessage:
    image_path: Path
    fallback_text: str
