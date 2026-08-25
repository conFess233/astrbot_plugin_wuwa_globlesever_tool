"""供外部集成共享的异步 HTTP Client。"""

import json
from collections.abc import Collection, Mapping
from typing import Any
from urllib.parse import urlparse

import aiohttp


class ResponseTooLargeError(ValueError):
    """表示解压后的 HTTP 响应超过调用方设置的安全上限。"""


async def read_limited_response(
    response: aiohttp.ClientResponse,
    max_bytes: int,
) -> bytes:
    """流式读取响应，避免先无界分配内存再检查大小。"""

    if max_bytes <= 0:
        raise ValueError("响应大小上限必须大于零")
    length = response.content_length
    if length is not None and length > max_bytes:
        raise ResponseTooLargeError("响应超过安全大小限制")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > max_bytes:
            raise ResponseTooLargeError("响应超过安全大小限制")
        chunks.append(chunk)
    return b"".join(chunks)


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
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("资源地址不受信任")
        if self.session is None or self.session.closed:
            raise RuntimeError("HTTP Client 尚未初始化")
        async with self.session.get(url, headers=headers) as response:
            if response.status != 200:
                raise ValueError(f"资源请求失败：HTTP {response.status}")
            try:
                return await read_limited_response(response, max_bytes)
            except ResponseTooLargeError as exc:
                raise ValueError("资源文件过大") from exc

    async def get_json(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        data = await self.get_bytes(
            url,
            allowed_hosts=allowed_hosts,
            max_bytes=max_bytes,
            headers=headers,
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
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("接口地址不受信任")
        if self.session is None or self.session.closed:
            raise RuntimeError("HTTP Client 尚未初始化")
        request_headers = dict(headers or {})
        request_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        async with self.session.post(url, json=body, headers=request_headers) as response:
            if response.status != 200:
                raise ValueError(f"接口请求失败：HTTP {response.status}")
            data = await read_limited_response(response, max_bytes)
            try:
                return json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("响应不是有效的 UTF-8 JSON") from exc

    def status(self) -> dict[str, Any]:
        return {
            "initialized": bool(self.session and not self.session.closed),
            "timeout_seconds": self.timeout_seconds,
        }
