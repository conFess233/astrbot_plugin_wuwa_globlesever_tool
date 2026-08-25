"""按来源优先级准备图片，并维护可回收的本地 LRU 缓存。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ...infrastructure.database import Database
from ...infrastructure.network import DownloadPolicy, SafeHttpDownloader
from .images import ImageInfo, inspect_image

_IMAGE_MAX_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CachedResource:
    resource_type: str
    resource_id: str
    source_url: str
    path: Path
    info: ImageInfo


class ResourceManager:
    def __init__(
        self,
        database: Database,
        cache_root: Path,
        downloader: SafeHttpDownloader,
        *,
        cache_limit_mb: int = 512,
        timeout_seconds: int = 30,
    ):
        self.database = database
        self.cache_root = cache_root.resolve()
        self.downloader = downloader
        self.cache_limit_bytes = cache_limit_mb * 1024 * 1024
        self.timeout_seconds = timeout_seconds
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def update_limits(self, *, cache_limit_mb: int, timeout_seconds: int) -> None:
        self.cache_limit_bytes = cache_limit_mb * 1024 * 1024
        self.timeout_seconds = timeout_seconds

    async def prepare_image(
        self,
        resource_type: str,
        resource_id: str,
        source_urls: Iterable[str | None],
        *,
        referenced: bool = False,
    ) -> CachedResource | None:
        key = (_safe_key(resource_type), _safe_key(resource_id))
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = await self._read_valid_entry(*key)
            urls = tuple(dict.fromkeys(url.strip() for url in source_urls if url and url.strip()))
            if cached is not None and (not urls or cached.source_url == urls[0]):
                await self._touch(*key, referenced=referenced)
                return cached
            for url in urls:
                try:
                    downloaded = await self.downloader.download(
                        url,
                        DownloadPolicy(
                            max_bytes=_IMAGE_MAX_BYTES,
                            timeout_seconds=self.timeout_seconds,
                        ),
                    )
                    info = inspect_image(downloaded.data)
                    digest = hashlib.sha256(downloaded.data).hexdigest()
                    target = self.cache_root / key[0] / f"{key[1]}-{digest[:16]}.{info.extension}"
                    await asyncio.to_thread(_write_atomic, target, downloaded.data)
                    await self._store_ready(
                        *key,
                        source_url=downloaded.final_url,
                        path=target,
                        size=len(downloaded.data),
                        version=digest,
                        referenced=referenced,
                        info=info,
                    )
                    if cached is not None and cached.path != target:
                        await asyncio.to_thread(cached.path.unlink, missing_ok=True)
                    await self.cleanup()
                    return CachedResource(key[0], key[1], downloaded.final_url, target, info)
                except (OSError, RuntimeError, ValueError):
                    continue
            if cached is not None:
                await self._touch(*key, referenced=referenced, stale=True)
                return cached
            await self._store_failed(*key, source_url=urls[0] if urls else "")
            return None

    async def mark_referenced(self, resource_type: str, resource_id: str, value: bool) -> None:
        await self.database.write(
            lambda db: db.execute(
                "UPDATE resource_entries SET referenced = ?, updated_at = ? "
                "WHERE resource_type = ? AND resource_id = ?",
                (
                    int(value),
                    _now(),
                    _safe_key(resource_type),
                    _safe_key(resource_id),
                ),
            )
        )

    async def cleanup(self, *, limit_bytes: int | None = None) -> dict[str, int]:
        limit = self.cache_limit_bytes if limit_bytes is None else max(0, limit_bytes)
        rows = await self.database.read(
            lambda db: db.execute(
                "SELECT resource_type, resource_id, cache_path, size_bytes, referenced "
                "FROM resource_entries WHERE status IN ('ready', 'stale') "
                "ORDER BY referenced DESC, last_accessed_at DESC"
            ).fetchall()
        )
        total = sum(max(0, int(row["size_bytes"])) for row in rows)
        removed_files = 0
        removed_bytes = 0
        if total <= limit:
            return {"removed_files": 0, "removed_bytes": 0, "remaining_bytes": total}
        for row in reversed(rows):
            if total <= limit:
                break
            if bool(row["referenced"]):
                continue
            path = self._safe_cache_path(str(row["cache_path"]))
            size = max(0, int(row["size_bytes"]))
            if path is not None:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            await self.database.write(
                lambda db, item=row: db.execute(
                    "DELETE FROM resource_entries WHERE resource_type = ? AND resource_id = ?",
                    (item["resource_type"], item["resource_id"]),
                )
            )
            total -= size
            removed_files += 1
            removed_bytes += size
        return {
            "removed_files": removed_files,
            "removed_bytes": removed_bytes,
            "remaining_bytes": total,
        }

    async def status(self) -> dict[str, int]:
        def operation(db: sqlite3.Connection) -> dict[str, int]:
            row = db.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS bytes, "
                "COALESCE(SUM(referenced), 0) AS referenced "
                "FROM resource_entries WHERE status IN ('ready', 'stale')"
            ).fetchone()
            return {
                "count": int(row["count"]),
                "bytes": int(row["bytes"]),
                "referenced": int(row["referenced"]),
                "limit_bytes": self.cache_limit_bytes,
            }

        return await self.database.read(operation)

    async def _read_valid_entry(
        self, resource_type: str, resource_id: str
    ) -> CachedResource | None:
        row = await self.database.read(
            lambda db: db.execute(
                "SELECT * FROM resource_entries WHERE resource_type = ? AND resource_id = ?",
                (resource_type, resource_id),
            ).fetchone()
        )
        if row is None or str(row["status"]) not in {"ready", "stale"}:
            return None
        path = self._safe_cache_path(str(row["cache_path"]))
        if path is None or not path.is_file():
            return None
        try:
            data = await asyncio.to_thread(path.read_bytes)
            if len(data) > _IMAGE_MAX_BYTES:
                return None
            info = inspect_image(data)
        except (OSError, ValueError):
            return None
        return CachedResource(
            resource_type,
            resource_id,
            str(row["source_url"]),
            path,
            info,
        )

    async def _store_ready(
        self,
        resource_type: str,
        resource_id: str,
        *,
        source_url: str,
        path: Path,
        size: int,
        version: str,
        referenced: bool,
        info: ImageInfo,
    ) -> None:
        now = _now()
        relative = await asyncio.to_thread(
            lambda: path.resolve().relative_to(self.cache_root).as_posix()
        )
        metadata = json.dumps(
            {"width": info.width, "height": info.height, "mime_type": info.mime_type},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await self.database.write(
            lambda db: db.execute(
                "INSERT INTO resource_entries "
                "(resource_type, resource_id, source_url, cache_path, version, size_bytes, "
                "status, referenced, last_accessed_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?) "
                "ON CONFLICT(resource_type, resource_id) DO UPDATE SET "
                "source_url = excluded.source_url, cache_path = excluded.cache_path, "
                "version = excluded.version, size_bytes = excluded.size_bytes, "
                "status = 'ready', referenced = excluded.referenced, "
                "last_accessed_at = excluded.last_accessed_at, updated_at = excluded.updated_at, "
                "metadata_json = excluded.metadata_json",
                (
                    resource_type,
                    resource_id,
                    source_url,
                    relative,
                    version,
                    size,
                    int(referenced),
                    now,
                    now,
                    metadata,
                ),
            )
        )

    async def _store_failed(self, resource_type: str, resource_id: str, source_url: str) -> None:
        now = _now()
        await self.database.write(
            lambda db: db.execute(
                "INSERT INTO resource_entries "
                "(resource_type, resource_id, source_url, cache_path, size_bytes, status, "
                "last_accessed_at, updated_at) VALUES (?, ?, ?, '', 0, 'failed', ?, ?) "
                "ON CONFLICT(resource_type, resource_id) DO UPDATE SET "
                "source_url = excluded.source_url, status = 'failed', "
                "last_accessed_at = excluded.last_accessed_at, updated_at = excluded.updated_at",
                (resource_type, resource_id, source_url, now, now),
            )
        )

    async def _touch(
        self,
        resource_type: str,
        resource_id: str,
        *,
        referenced: bool,
        stale: bool = False,
    ) -> None:
        await self.database.write(
            lambda db: db.execute(
                "UPDATE resource_entries SET last_accessed_at = ?, referenced = ?, "
                "status = CASE WHEN ? THEN 'stale' ELSE status END "
                "WHERE resource_type = ? AND resource_id = ?",
                (_now(), int(referenced), int(stale), resource_type, resource_id),
            )
        )

    def _safe_cache_path(self, relative: str) -> Path | None:
        if not relative:
            return None
        try:
            path = (self.cache_root / relative).resolve()
            path.relative_to(self.cache_root)
        except (OSError, ValueError):
            return None
        return path


def _safe_key(value: str) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("资源标识无效")
    if re.fullmatch(r"[A-Za-z0-9_-]+", normalized):
        return normalized
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
