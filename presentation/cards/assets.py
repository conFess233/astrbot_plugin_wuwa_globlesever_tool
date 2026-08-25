"""在进入无网络渲染阶段前，将远程图片和默认字体固化为 data URI。"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from ..resources import FontManager, ResourceManager, UiAssetManifest


class CardAssetPreparer:
    def __init__(
        self,
        resources: ResourceManager,
        fonts: FontManager,
        manifest: UiAssetManifest,
    ):
        self.resources = resources
        self.fonts = fonts
        self.manifest = manifest

    async def prepare(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = _copy_json_tree(payload)
        jobs: list[Awaitable[None]] = []
        player = result.get("player")
        if isinstance(player, dict):
            jobs.append(self._prepare_player(player))
        characters = result.get("characters")
        if isinstance(characters, list):
            jobs.extend(
                self._prepare_character(item) for item in characters if isinstance(item, dict)
            )
        character = result.get("character")
        if isinstance(character, dict):
            jobs.append(self._prepare_character(character, detail=True))
        if jobs:
            semaphore = asyncio.Semaphore(4)

            async def limited(job: Awaitable[None]) -> None:
                async with semaphore:
                    await job

            await asyncio.gather(*(limited(job) for job in jobs))
        result["theme"] = await self._theme()
        return result

    async def _prepare_player(self, player: dict[str, Any]) -> None:
        avatar_id = str(player.get("avatar_id") or "")
        game_avatar_url = self.manifest.account_avatar_url(avatar_id) if avatar_id else None
        resource_id = f"{player.get('uid') or 'unknown'}-{avatar_id or 'default'}"
        await self._image(
            player,
            "image_url",
            "avatar",
            resource_id,
            (player.get("image_url"), game_avatar_url, player.get("qq_avatar_url")),
            "avatar",
        )

    async def _prepare_character(self, character: dict[str, Any], *, detail: bool = False) -> None:
        character_id = str(character.get("character_id") or "unknown")
        fallback = self.manifest.character(character_id)
        await self._image(
            character,
            "image_url",
            "character-card",
            character_id,
            (
                character.get("image_url"),
                fallback.get("card_picture_url") if fallback else None,
            ),
            "character",
        )
        if not detail:
            return
        await self._image(
            character,
            "illustration_picture_url",
            "character-illustration",
            character_id,
            (
                character.get("illustration_picture_url"),
                fallback.get("illustration_picture_url") if fallback else None,
            ),
            "character",
        )
        element_id = str(character.get("element_id") or "unknown")
        element = next(
            (item for item in self.manifest.elements if str(item.get("id")) == element_id),
            None,
        )
        await self._image(
            character,
            "element_image_url",
            "element",
            element_id,
            (
                character.get("element_image_url"),
                element.get("picture_url") if element else None,
            ),
            "element",
        )
        weapon_id = str(character.get("weapon_id") or "unknown")
        await self._image(
            character,
            "weapon_image_url",
            "weapon",
            weapon_id,
            (character.get("weapon_image_url"),),
            "weapon",
        )
        weapon_type_id = str(character.get("weapon_type_id") or weapon_id[:4] or "unknown")
        await self._image(
            character,
            "weapon_type_image_url",
            "weapon-type",
            weapon_type_id,
            (character.get("weapon_type_image_url"),),
            "element",
        )

    async def _image(
        self,
        container: dict[str, Any],
        key: str,
        resource_type: str,
        resource_id: str,
        urls: tuple[object, ...],
        placeholder: str,
    ) -> None:
        candidates = tuple(value for value in urls if isinstance(value, str) and value)
        cached = await self.resources.prepare_image(
            resource_type,
            resource_id,
            candidates,
            referenced=True,
        )
        if cached is not None:
            container[key] = await asyncio.to_thread(
                _file_data_uri, cached.path, cached.info.mime_type
            )
            return
        container[key] = await asyncio.to_thread(self._placeholder_data_uri, placeholder)

    def _placeholder_data_uri(self, name: str) -> str:
        placeholders = self.manifest.payload.get("placeholders", {})
        relative = str(placeholders.get(name) or "") if isinstance(placeholders, dict) else ""
        path = (self.manifest.path.parent / relative).resolve()
        static_root = self.manifest.path.parent.parent.resolve()
        try:
            path.relative_to(static_root)
        except ValueError:
            return ""
        if not path.is_file() or path.suffix.casefold() != ".svg":
            return ""
        return _file_data_uri(path, "image/svg+xml")

    async def _theme(self) -> dict[str, str]:
        font = await self.fonts.default_font()
        if font is None:
            return {"font_family": "", "font_data_uri": ""}
        try:
            mime = "font/otf" if font.path.suffix.casefold() == ".otf" else "font/ttf"
            uri = await asyncio.to_thread(_file_data_uri, font.path, mime)
        except OSError:
            return {"font_family": "", "font_data_uri": ""}
        return {"font_family": font.display_name, "font_data_uri": uri}


def _file_data_uri(path: Path, mime_type: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _copy_json_tree(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _copy_json_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json_tree(item) for item in value]
    return value
