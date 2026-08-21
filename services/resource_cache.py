"""攻略站固定图片的受限下载、校验与本地缓存。"""

import asyncio
import base64
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path

_MAX_RESOURCE_BYTES = 5 * 1024 * 1024
_IMAGE_TYPES = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"RIFF", "webp", "image/webp"),
)

ImageFetch = Callable[[str, int], Awaitable[bytes]]


class StaticImageCache:
    def __init__(self, directory: Path, fetch: ImageFetch):
        self.directory = directory
        self.fetch = fetch
        self._locks: dict[str, asyncio.Lock] = {}

    async def data_uri(self, url: str) -> str | None:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cached = await asyncio.to_thread(self._find_cached, digest)
        if cached is None:
            lock = self._locks.setdefault(digest, asyncio.Lock())
            async with lock:
                cached = await asyncio.to_thread(self._find_cached, digest)
                if cached is None:
                    try:
                        data = await self.fetch(url, _MAX_RESOURCE_BYTES)
                        extension, _ = _identify_image(data)
                        await asyncio.to_thread(self.directory.mkdir, parents=True, exist_ok=True)
                        cached = self.directory / f"{digest}.{extension}"
                        await asyncio.to_thread(_write_atomic, cached, data)
                    except (OSError, RuntimeError, ValueError):
                        return None
        try:
            data = await asyncio.to_thread(cached.read_bytes)
            if len(data) > _MAX_RESOURCE_BYTES:
                return None
            _, mime = _identify_image(data)
        except (OSError, ValueError):
            return None
        return await asyncio.to_thread(_data_uri, data, mime)

    def _find_cached(self, digest: str) -> Path | None:
        for extension in ("png", "jpg", "webp"):
            candidate = self.directory / f"{digest}.{extension}"
            if candidate.is_file():
                try:
                    data = candidate.read_bytes()
                    _identify_image(data)
                    if len(data) <= _MAX_RESOURCE_BYTES:
                        return candidate
                except (OSError, ValueError):
                    continue
        return None


def _identify_image(data: bytes) -> tuple[str, str]:
    for signature, extension, mime in _IMAGE_TYPES:
        if not data.startswith(signature):
            continue
        if extension == "webp" and (len(data) < 12 or data[8:12] != b"WEBP"):
            continue
        return extension, mime
    raise ValueError("资源不是受支持的 PNG、JPEG 或 WebP 图片")


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _data_uri(data: bytes, mime: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"
