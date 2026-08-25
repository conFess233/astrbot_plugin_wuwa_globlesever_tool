"""自定义字体下载、ZIP 安全拆包、校验与默认字体登记。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import stat
import struct
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from ...infrastructure.database import Database
from ...infrastructure.network import DownloadPolicy, SafeHttpDownloader

_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 256
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_MAX_FONT_BYTES = 32 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100
_SFNT_SIGNATURES = {b"\x00\x01\x00\x00", b"true", b"typ1", b"OTTO"}


class FontPackageError(ValueError):
    """表示字体文件或压缩包未通过安全与格式校验。"""


@dataclass(frozen=True, slots=True)
class FontEntry:
    font_id: str
    display_name: str
    source_url: str | None
    path: Path
    weight: int
    style: str
    is_default: bool
    installed_at: str


@dataclass(frozen=True, slots=True)
class _FontMetadata:
    family: str
    subfamily: str
    weight: int
    extension: str


class FontManager:
    def __init__(
        self,
        database: Database,
        fonts_root: Path,
        downloader: SafeHttpDownloader,
        *,
        timeout_seconds: int = 60,
    ):
        self.database = database
        self.fonts_root = fonts_root.resolve()
        self.downloader = downloader
        self.timeout_seconds = timeout_seconds

    async def install_from_url(
        self,
        url: str,
        *,
        display_name: str | None = None,
        make_default: bool = False,
    ) -> tuple[FontEntry, ...]:
        downloaded = await self.downloader.download(
            url,
            DownloadPolicy(
                max_bytes=_MAX_DOWNLOAD_BYTES,
                timeout_seconds=self.timeout_seconds,
                max_redirects=3,
            ),
        )
        name = PurePosixPath(urlsplit(downloaded.final_url).path).name.casefold()
        is_zip = downloaded.data.startswith(b"PK\x03\x04") or name.endswith(".zip")
        if is_zip:
            files = await asyncio.to_thread(_read_safe_zip_fonts, downloaded.data)
        elif name.endswith((".rar", ".7z", ".tar", ".tar.gz", ".tgz")):
            raise FontPackageError("仅支持直接 TTF/OTF 文件或 ZIP 压缩包")
        else:
            files = ((name or "font", downloaded.data),)
        if make_default and len(files) > 1:
            files = tuple(sorted(files, key=_default_font_candidate_priority))
        entries: list[FontEntry] = []
        for index, (file_name, data) in enumerate(files):
            custom_name = display_name if len(files) == 1 else None
            entry = await self._install_font(
                file_name,
                data,
                source_url=downloaded.final_url,
                display_name=custom_name,
                make_default=make_default and index == 0,
            )
            entries.append(entry)
        if not entries:
            raise FontPackageError("ZIP 中没有可安装的 TTF/OTF 字体")
        return tuple(entries)

    async def list_fonts(self) -> tuple[FontEntry, ...]:
        rows = await self.database.read(
            lambda db: db.execute(
                "SELECT * FROM font_entries ORDER BY is_default DESC, display_name, weight"
            ).fetchall()
        )
        result: list[FontEntry] = []
        for row in rows:
            path = self._safe_font_path(str(row["font_path"]))
            if path is None or not path.is_file():
                continue
            result.append(_entry_from_row(row, path))
        return tuple(result)

    async def default_font(self) -> FontEntry | None:
        rows = await self.list_fonts()
        return next((item for item in rows if item.is_default), None)

    async def set_default(self, font_id: str) -> FontEntry:
        def operation(db):
            row = db.execute("SELECT * FROM font_entries WHERE font_id = ?", (font_id,)).fetchone()
            if row is None:
                raise FontPackageError("字体不存在")
            db.execute("UPDATE font_entries SET is_default = 0 WHERE is_default = 1")
            db.execute("UPDATE font_entries SET is_default = 1 WHERE font_id = ?", (font_id,))
            return db.execute("SELECT * FROM font_entries WHERE font_id = ?", (font_id,)).fetchone()

        row = await self.database.write(operation)
        path = self._safe_font_path(str(row["font_path"]))
        if path is None or not path.is_file():
            raise FontPackageError("字体文件已丢失")
        return _entry_from_row(row, path)

    async def delete(self, font_id: str) -> None:
        def operation(db):
            row = db.execute(
                "SELECT font_path, is_default FROM font_entries WHERE font_id = ?", (font_id,)
            ).fetchone()
            if row is None:
                raise FontPackageError("字体不存在")
            if bool(row["is_default"]):
                raise FontPackageError("当前默认字体不能删除，请先切换默认字体")
            db.execute("DELETE FROM font_entries WHERE font_id = ?", (font_id,))
            return str(row["font_path"])

        relative = await self.database.write(operation)
        path = self._safe_font_path(relative)
        if path is not None:
            await asyncio.to_thread(path.unlink, missing_ok=True)

    async def _install_font(
        self,
        file_name: str,
        data: bytes,
        *,
        source_url: str | None,
        display_name: str | None,
        make_default: bool,
    ) -> FontEntry:
        if not 0 < len(data) <= _MAX_FONT_BYTES:
            raise FontPackageError("字体文件大小无效")
        metadata = _inspect_sfnt(data)
        font_id = hashlib.sha256(data).hexdigest()
        target = self.fonts_root / "installed" / f"{font_id}.{metadata.extension}"
        await asyncio.to_thread(_write_atomic, target, data)
        shown_name = (display_name or metadata.family).strip()[:120]
        if not shown_name:
            shown_name = Path(file_name).stem[:120] or "自定义字体"
        installed_at = _now()
        relative = target.resolve().relative_to(self.fonts_root).as_posix()
        style = (
            "italic"
            if any(word in metadata.subfamily.casefold() for word in ("italic", "oblique"))
            else "normal"
        )
        metadata_json = json.dumps(
            {
                "internal_family": metadata.family,
                "internal_subfamily": metadata.subfamily,
                "original_file_name": PurePosixPath(file_name).name,
                "sha256": font_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        def operation(db):
            if make_default:
                db.execute("UPDATE font_entries SET is_default = 0 WHERE is_default = 1")
            db.execute(
                "INSERT INTO font_entries "
                "(font_id, display_name, source_url, font_path, weight, style, is_default, "
                "installed_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(font_id) DO UPDATE SET display_name = excluded.display_name, "
                "source_url = excluded.source_url, font_path = excluded.font_path, "
                "weight = excluded.weight, style = excluded.style, "
                "is_default = CASE WHEN excluded.is_default = 1 THEN 1 "
                "ELSE font_entries.is_default END, "
                "metadata_json = excluded.metadata_json",
                (
                    font_id,
                    shown_name,
                    source_url,
                    relative,
                    metadata.weight,
                    style,
                    int(make_default),
                    installed_at,
                    metadata_json,
                ),
            )
            return db.execute("SELECT * FROM font_entries WHERE font_id = ?", (font_id,)).fetchone()

        try:
            row = await self.database.write(operation)
        except Exception:
            await asyncio.to_thread(target.unlink, missing_ok=True)
            raise
        return _entry_from_row(row, target)

    def _safe_font_path(self, relative: str) -> Path | None:
        try:
            path = (self.fonts_root / relative).resolve()
            path.relative_to(self.fonts_root)
        except (OSError, ValueError):
            return None
        return path


def _read_safe_zip_fonts(data: bytes) -> tuple[tuple[str, bytes], ...]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise FontPackageError("ZIP 压缩包损坏") from exc
    with archive:
        members = archive.infolist()
        if len(members) > _MAX_ARCHIVE_MEMBERS:
            raise FontPackageError("ZIP 文件数量超过安全限制")
        total = 0
        selected: list[zipfile.ZipInfo] = []
        for member in members:
            path = PurePosixPath(member.filename.replace("\\", "/"))
            mode = member.external_attr >> 16
            if (
                path.is_absolute()
                or ".." in path.parts
                or any(":" in part for part in path.parts)
                or len(path.parts) > 8
                or stat.S_ISLNK(mode)
            ):
                raise FontPackageError("ZIP 包含不安全路径或符号链接")
            if member.is_dir():
                continue
            total += member.file_size
            if total > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise FontPackageError("ZIP 解压后大小超过安全限制")
            if member.file_size > _MAX_FONT_BYTES:
                raise FontPackageError("ZIP 中单个文件过大")
            if member.file_size and (
                member.compress_size == 0
                or member.file_size / member.compress_size > _MAX_COMPRESSION_RATIO
            ):
                raise FontPackageError("ZIP 压缩率异常")
            if path.suffix.casefold() in {".ttf", ".otf"}:
                selected.append(member)
        files: list[tuple[str, bytes]] = []
        for member in selected:
            try:
                content = archive.read(member)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise FontPackageError("ZIP 字体读取失败") from exc
            if len(content) != member.file_size:
                raise FontPackageError("ZIP 字体大小与目录记录不一致")
            _inspect_sfnt(content)
            files.append((member.filename, content))
        return tuple(files)


def _default_font_candidate_priority(item: tuple[str, bytes]) -> tuple[bool, bool, str]:
    normalized = item[0].casefold().replace("\\", "/")
    tokens = normalized.replace("/", "_").replace("-", "_").replace(" ", "_").split("_")
    return ("sc" not in tokens, "regular" not in tokens, normalized)


def _inspect_sfnt(data: bytes) -> _FontMetadata:
    if len(data) < 12 or data[:4] not in _SFNT_SIGNATURES:
        raise FontPackageError("文件不是有效的 TTF/OTF 字体")
    table_count = struct.unpack_from(">H", data, 4)[0]
    if not 0 < table_count <= 256 or 12 + table_count * 16 > len(data):
        raise FontPackageError("字体表目录无效")
    tables: dict[bytes, tuple[int, int]] = {}
    for index in range(table_count):
        offset = 12 + index * 16
        tag, _, table_offset, length = struct.unpack_from(">4sIII", data, offset)
        if table_offset > len(data) or length > len(data) - table_offset:
            raise FontPackageError("字体表越界")
        tables[tag] = (table_offset, length)
    if b"name" not in tables:
        raise FontPackageError("字体缺少名称表")
    family, subfamily = _read_name_table(data, *tables[b"name"])
    weight = 400
    os2 = tables.get(b"OS/2")
    if os2 is not None and os2[1] >= 6:
        weight = struct.unpack_from(">H", data, os2[0] + 4)[0]
        if not 1 <= weight <= 1000:
            weight = 400
    extension = "otf" if data[:4] == b"OTTO" else "ttf"
    return _FontMetadata(family or "自定义字体", subfamily or "Regular", weight, extension)


def _read_name_table(data: bytes, table_offset: int, table_length: int) -> tuple[str, str]:
    if table_length < 6:
        raise FontPackageError("字体名称表不完整")
    _, count, strings_offset = struct.unpack_from(">HHH", data, table_offset)
    records_end = 6 + count * 12
    if count > 4096 or records_end > table_length or strings_offset > table_length:
        raise FontPackageError("字体名称表无效")
    candidates: dict[int, list[tuple[int, str]]] = {1: [], 2: [], 16: [], 17: []}
    for index in range(count):
        record_offset = table_offset + 6 + index * 12
        platform, encoding, language, name_id, length, offset = struct.unpack_from(
            ">HHHHHH", data, record_offset
        )
        if name_id not in candidates:
            continue
        start = table_offset + strings_offset + offset
        end = start + length
        if start < table_offset or end > table_offset + table_length:
            continue
        raw = data[start:end]
        try:
            if platform in {0, 3}:
                value = raw.decode("utf-16-be")
            elif platform == 1:
                value = raw.decode("mac_roman")
            else:
                continue
        except UnicodeDecodeError:
            continue
        value = value.replace("\x00", "").strip()
        if value:
            priority = 0 if platform == 3 and language in {0x0409, 0x0804} else 1
            priority += 0 if encoding in {1, 10} else 1
            candidates[name_id].append((priority, value))

    def choose(*ids: int) -> str:
        for name_id in ids:
            if candidates[name_id]:
                return min(candidates[name_id], key=lambda item: item[0])[1]
        return ""

    return choose(16, 1), choose(17, 2)


def _entry_from_row(row, path: Path) -> FontEntry:
    return FontEntry(
        font_id=str(row["font_id"]),
        display_name=str(row["display_name"]),
        source_url=str(row["source_url"]) if row["source_url"] else None,
        path=path,
        weight=int(row["weight"]),
        style=str(row["style"]),
        is_default=bool(row["is_default"]),
        installed_at=str(row["installed_at"]),
    )


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(UTC).isoformat()
