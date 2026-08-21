"""随插件发布的稳定角色目录及严格名称解析。"""

import json
from dataclasses import dataclass
from pathlib import Path


class CatalogError(ValueError):
    """表示静态目录损坏、缺失或查询无法唯一解析。"""


@dataclass(frozen=True, slots=True)
class CharacterDefinition:
    character_id: str
    names: dict[str, str]
    card_picture_url: str | None = None
    illustration_picture_url: str | None = None
    star: int | None = None
    element_id: str | None = None
    element_picture_url: str | None = None

    @property
    def display_name(self) -> str:
        return self.names.get("zh-CN") or self.names.get("en") or self.character_id


class CharacterCatalog:
    def __init__(self, characters: tuple[CharacterDefinition, ...]):
        self.characters = characters
        self._by_id = {item.character_id: item for item in characters}
        self._by_name: dict[str, list[CharacterDefinition]] = {}
        for item in characters:
            normalized_names = {name.strip().casefold() for name in item.names.values()}
            for name in normalized_names:
                self._by_name.setdefault(name, []).append(item)

    @classmethod
    def load_bundled(cls, runtime_override: Path | None = None) -> "CharacterCatalog":
        bundled = Path(__file__).resolve().parent.parent / "assets" / "static" / "characters.json"
        path = (
            runtime_override
            if runtime_override is not None and runtime_override.is_file()
            else bundled
        )
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise CatalogError("角色静态目录无法读取") from exc

        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: object) -> "CharacterCatalog":
        try:
            raw_characters = payload["characters"]
        except (KeyError, TypeError) as exc:
            raise CatalogError("角色静态目录格式无效") from exc
        if not isinstance(raw_characters, list):
            raise CatalogError("角色静态目录格式无效")

        characters: list[CharacterDefinition] = []
        seen_ids: set[str] = set()
        for raw in raw_characters:
            character_id = str(raw.get("id", "")).strip()
            names = {
                str(key): str(value).strip()
                for key, value in raw.items()
                if key in {"zh-CN", "zh-TW", "en", "ja", "ko"} and str(value).strip()
            }
            if not character_id or not names or character_id in seen_ids:
                raise CatalogError("角色静态目录包含无效或重复记录")
            seen_ids.add(character_id)
            star = raw.get("star")
            if star is not None and (isinstance(star, bool) or not isinstance(star, int)):
                raise CatalogError("角色静态目录包含无效星级")
            characters.append(
                CharacterDefinition(
                    character_id=character_id,
                    names=names,
                    card_picture_url=_optional_text(raw.get("card_picture_url")),
                    illustration_picture_url=_optional_text(raw.get("illustration_picture_url")),
                    star=star,
                    element_id=_optional_text(raw.get("element_id")),
                    element_picture_url=_optional_text(raw.get("element_picture_url")),
                )
            )
        if not characters:
            raise CatalogError("角色静态目录为空")
        return cls(tuple(characters))

    def resolve(self, query: str) -> CharacterDefinition:
        normalized = query.strip()
        if not normalized:
            raise CatalogError("角色不能为空")
        by_id = self._by_id.get(normalized)
        if by_id:
            return by_id
        matches = self._by_name.get(normalized.casefold(), [])
        if not matches:
            raise CatalogError(f"未找到角色：{normalized}")
        if len(matches) > 1:
            candidates = "、".join(item.display_name for item in matches)
            raise CatalogError(f"角色名称存在歧义：{candidates}")
        return matches[0]

    def get(self, character_id: str | int | None) -> CharacterDefinition | None:
        if character_id is None:
            return None
        return self._by_id.get(str(character_id))


def _optional_text(value: object) -> str | None:
    result = str(value or "").strip()
    return result or None
