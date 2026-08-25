"""使用统一模板渲染无网络、可缓存的查询长图。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...domain.cards import CardViewModel
from .assets import CardAssetPreparer
from .postprocess import trim_card_canvas

_RENDER_SCHEMA = "wuwa-card-v3"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_CARD_BYTES = 32 * 1024 * 1024

HtmlRender = Callable[[str, dict[str, object], dict[str, object]], Awaitable[str]]


class CardRenderError(RuntimeError):
    """表示 AstrBot HTML 渲染或渲染缓存不可用。"""


class AstrBotCardRenderer:
    def __init__(
        self,
        template_path: Path,
        stylesheet_path: Path,
        cache_directory: Path,
        html_render: HtmlRender,
        timeout_seconds: int,
        assets: CardAssetPreparer,
    ):
        self.template_path = template_path
        self.stylesheet_path = stylesheet_path
        self.cache_directory = cache_directory
        self.html_render = html_render
        self.timeout_seconds = timeout_seconds
        self.assets = assets
        self._locks: dict[str, asyncio.Lock] = {}

    async def render(self, view_model: CardViewModel) -> Path:
        try:
            payload = await asyncio.wait_for(
                self.assets.prepare(asdict(view_model)), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise CardRenderError("卡片资源准备超时") from exc
        template, stylesheet = await asyncio.gather(
            asyncio.to_thread(self.template_path.read_text, encoding="utf-8"),
            asyncio.to_thread(self.stylesheet_path.read_text, encoding="utf-8"),
        )
        document = template.replace("/*__WUWA_CARD_STYLES__*/", stylesheet)
        digest = hashlib.sha256(
            json.dumps(
                {"schema": _RENDER_SCHEMA, "payload": payload, "document": document},
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
                rendered_path = await asyncio.wait_for(
                    self._render_uncached(document, payload),
                    timeout=self.timeout_seconds,
                )
                source = await asyncio.to_thread(_absolute_path, rendered_path)
                if not await asyncio.to_thread(_valid_png, source):
                    raise CardRenderError("AstrBot 返回的图片文件无效")
                await asyncio.to_thread(self.cache_directory.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copyfile, source, target)
                await asyncio.to_thread(trim_card_canvas, target)
                await asyncio.to_thread(self._prune_scope, scope, target)
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                raise CardRenderError("图片渲染失败") from exc
            if not await asyncio.to_thread(_valid_png, target):
                raise CardRenderError("图片缓存写入失败")
            return target

    async def _render_uncached(self, document: str, payload: dict[str, Any]) -> str:
        return await self.html_render(
            document,
            payload,
            {
                "type": "png",
                "full_page": True,
                "animations": "disabled",
                "scale": "css",
            },
        )

    def _prune_scope(self, scope: str, keep: Path) -> None:
        for candidate in self.cache_directory.glob(f"{scope}-*.png"):
            if candidate != keep and candidate.parent == self.cache_directory:
                candidate.unlink(missing_ok=True)


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
