"""带 SSRF、重定向与响应大小防护的 HTTP(S) 下载器。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver


class UnsafeUrlError(ValueError):
    """表示下载地址可能访问非公网网络或格式不安全。"""


@dataclass(frozen=True, slots=True)
class DownloadPolicy:
    max_bytes: int
    timeout_seconds: int = 30
    max_redirects: int = 3

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("下载大小限制必须大于 0")
        if self.timeout_seconds <= 0:
            raise ValueError("下载超时必须大于 0")
        if not 0 <= self.max_redirects <= 10:
            raise ValueError("重定向次数限制无效")


@dataclass(frozen=True, slots=True)
class DownloadedResource:
    data: bytes
    final_url: str
    content_type: str


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_global


def _normalized_url(url: str) -> tuple[str, str]:
    parsed = urlsplit(url.strip())
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise UnsafeUrlError("仅允许 HTTP 或 HTTPS 下载地址")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("下载地址主机格式无效")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("下载地址端口无效") from exc
    host = parsed.hostname.rstrip(".").casefold()
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise UnsafeUrlError("下载地址不能指向本机")
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise UnsafeUrlError("下载地址不能指向非公网网络")
    if parsed.fragment:
        parsed = parsed._replace(fragment="")
    hostname = f"[{host}]" if ":" in host else host
    netloc = f"{hostname}:{port}" if port is not None else hostname
    return urlunsplit(
        (parsed.scheme.casefold(), netloc, parsed.path or "/", parsed.query, "")
    ), host


async def validate_public_url(url: str) -> str:
    """解析一次地址并验证所有解析结果均为公网 IP。"""

    normalized, host = _normalized_url(url)
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            results = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeUrlError("下载地址无法解析") from exc
        addresses = {str(item[4][0]) for item in results}
        if not addresses or any(not _is_public_address(item) for item in addresses):
            raise UnsafeUrlError("下载地址解析到了非公网网络") from None
    return normalized


class _PublicNetworkResolver(AbstractResolver):
    def __init__(self) -> None:
        self._resolver = aiohttp.DefaultResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        records = await self._resolver.resolve(host, port, family)
        if not records or any(not _is_public_address(str(item["host"])) for item in records):
            raise OSError("拒绝连接到非公网网络")
        return records

    async def close(self) -> None:
        await self._resolver.close()


class SafeHttpDownloader:
    """连接阶段再次校验 DNS，并逐跳验证重定向目标。"""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def initialize(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        connector = aiohttp.TCPConnector(
            resolver=_PublicNetworkResolver(),
            ttl_dns_cache=60,
            limit=8,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            raise_for_status=False,
            trust_env=False,
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def download(self, url: str, policy: DownloadPolicy) -> DownloadedResource:
        if self._session is None or self._session.closed:
            await self.initialize()
        assert self._session is not None
        current = await validate_public_url(url)
        timeout = aiohttp.ClientTimeout(total=policy.timeout_seconds)
        for redirect_count in range(policy.max_redirects + 1):
            try:
                async with self._session.get(
                    current,
                    allow_redirects=False,
                    timeout=timeout,
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise ValueError("重定向响应缺少目标地址")
                        if redirect_count >= policy.max_redirects:
                            raise ValueError("下载重定向次数过多")
                        current = await validate_public_url(urljoin(current, location))
                        continue
                    if response.status != 200:
                        raise ValueError(f"下载失败：HTTP {response.status}")
                    length = response.content_length
                    if length is not None and length > policy.max_bytes:
                        raise ValueError("下载内容超过大小限制")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > policy.max_bytes:
                            raise ValueError("下载内容超过大小限制")
                        chunks.append(chunk)
                    return DownloadedResource(
                        data=b"".join(chunks),
                        final_url=current,
                        content_type=response.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .strip()
                        .casefold(),
                    )
            except aiohttp.ClientError as exc:
                raise ValueError("下载连接失败") from exc
        raise ValueError("下载重定向次数过多")
