"""从角色快照机械生成 UI 资源清单与多语言别名表。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHARACTERS_PATH = ROOT / "static" / "data" / "characters.json"
STATIC_DATA = ROOT / "static" / "data"


def main() -> None:
    source = json.loads(CHARACTERS_PATH.read_text(encoding="utf-8-sig"))
    characters = source["characters"]
    elements_by_id: dict[str, dict[str, str]] = {}
    manifest_characters: list[dict[str, object]] = []
    aliases: dict[str, str] = {}
    for item in characters:
        character_id = str(item["id"])
        element_id = str(item.get("element_id") or "")
        element_url = str(item.get("element_picture_url") or "")
        if element_id and element_url:
            elements_by_id[element_id] = {"id": element_id, "picture_url": element_url}
        names = {
            locale: str(item.get(locale) or "") for locale in ("zh-CN", "zh-TW", "en", "ja", "ko")
        }
        for name in names.values():
            normalized = " ".join(name.strip().casefold().split())
            if normalized:
                aliases.setdefault(normalized, character_id)
        manifest_characters.append(
            {
                "id": character_id,
                "names": names,
                "star": int(item.get("star") or 0),
                "element_id": element_id,
                "card_picture_url": str(item.get("card_picture_url") or ""),
                "illustration_picture_url": str(item.get("illustration_picture_url") or ""),
            }
        )
    manifest = {
        "schema_version": 1,
        "snapshot_date": source.get("snapshot_date"),
        "sources": [
            {
                "name": source.get("source"),
                "url": source.get("source_url"),
                "scope": "character-and-element-metadata",
            }
        ],
        "resolution_order": [
            "current_api_url",
            "guide_public_metadata",
            "bundled_manifest",
            "valid_local_cache",
            "local_placeholder",
        ],
        "placeholders": {
            "avatar": "../ui/placeholders/avatar.svg",
            "character": "../ui/placeholders/character.svg",
            "weapon": "../ui/placeholders/weapon.svg",
            "element": "../ui/placeholders/element.svg",
        },
        "ui": {
            "brand_mark": "../ui/icons/brand-mark.svg",
            "star": "../ui/icons/star.svg",
            "background_texture": "../ui/textures/card-grain.svg",
        },
        "elements": sorted(elements_by_id.values(), key=lambda item: int(item["id"])),
        "characters": manifest_characters,
        "weapons": {
            "mode": "runtime-discovered",
            "identity_source": "role-detail-api",
            "fields": [
                "weaponId",
                "weaponName",
                "weaponPictureUrl",
                "weaponStar",
                "weaponTypeId",
                "weaponTypePictureUrl",
            ],
            "placeholder": "../ui/placeholders/weapon.svg",
        },
    }
    alias_payload = {
        "schema_version": 1,
        "snapshot_date": source.get("snapshot_date"),
        "normalization": "unicode-casefold-and-collapsed-space",
        "characters": dict(sorted(aliases.items())),
        "weapons": {},
    }
    STATIC_DATA.mkdir(parents=True, exist_ok=True)
    (STATIC_DATA / "ui_assets.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (STATIC_DATA / "aliases.json").write_text(
        json.dumps(alias_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
