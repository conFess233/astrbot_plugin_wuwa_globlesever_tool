"""供外部集成共享的异步 HTTP Client。"""

import json
from collections.abc import Collection
from typing import Any
from urllib.parse import urlparse

import aiohttp


class HttpClient:
    def __init__(self, timeout_seconds: int):
        self.timeout_seconds = timeout_seconds
        self.session: aiohttp.ClientSession | None = None

    async def initialize(self) -> None:
        if self.session and not self.session.closed:
            return
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        self.session = aiohttp.ClientSession(timeout=timeout, raise_for_status=False)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def update_timeout(self, timeout_seconds: int) -> None:
        if timeout_seconds == self.timeout_seconds:
            return
        await self.close()
        self.timeout_seconds = timeout_seconds
        await self.initialize()

    async def get_bytes(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str],
        max_bytes: int,
    ) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("资源地址不受信任")
        if self.session is None or self.session.closed:
            raise RuntimeError("HTTP Client 尚未初始化")
        async with self.session.get(url) as response:
            if response.status != 200:
                raise ValueError(f"资源请求失败：HTTP {response.status}")
            length = response.content_length
            if length is not None and length > max_bytes:
                raise ValueError("资源文件过大")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("资源文件过大")
                chunks.append(chunk)
            return b"".join(chunks)

    async def get_json(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str],
        max_bytes: int,
    ) -> Any:
        data = await self.get_bytes(
            url,
            allowed_hosts=allowed_hosts,
            max_bytes=max_bytes,
        )
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("响应不是有效的 UTF-8 JSON") from exc

    async def post_json(
        self,
        url: str,
        body: dict[str, Any],
        *,
        allowed_hosts: Collection[str],
        max_bytes: int,
    ) -> Any:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("接口地址不受信任")
        if self.session is None or self.session.closed:
            raise RuntimeError("HTTP Client 尚未初始化")
        async with self.session.post(url, json=body) as response:
            if response.status != 200:
                raise ValueError(f"接口请求失败：HTTP {response.status}")
            data = await response.read()
            if len(data) > max_bytes:
                raise ValueError("接口响应超过安全大小限制")
            try:
                return json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("响应不是有效的 UTF-8 JSON") from exc

    def status(self) -> dict[str, Any]:
        return {
            "initialized": bool(self.session and not self.session.closed),
            "timeout_seconds": self.timeout_seconds,
        }
