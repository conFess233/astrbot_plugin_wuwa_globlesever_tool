"""只读加载并校验随插件发布的鸣潮 UI 资源清单。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class UiAssetManifest:
    path: Path
    payload: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> UiAssetManifest:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("UI 资源清单无法读取") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("UI 资源清单版本不受支持")
        characters = payload.get("characters")
        elements = payload.get("elements")
        if not isinstance(characters, list) or not isinstance(elements, list):
            raise ValueError("UI 资源清单结构无效")
        return cls(path=path, payload=payload)

    @property
    def characters(self) -> tuple[dict[str, Any], ...]:
        return tuple(item for item in self.payload["characters"] if isinstance(item, dict))

    @property
    def elements(self) -> tuple[dict[str, Any], ...]:
        return tuple(item for item in self.payload["elements"] if isinstance(item, dict))

    def character(self, character_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.characters if str(item.get("id")) == str(character_id)),
            None,
        )
