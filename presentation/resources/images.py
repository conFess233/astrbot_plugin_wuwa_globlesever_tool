"""不依赖图像扩展库的图片魔数与尺寸校验。"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ImageInfo:
    extension: str
    mime_type: str
    width: int
    height: int


def inspect_image(data: bytes, *, max_dimension: int = 8192) -> ImageInfo:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        info = _png_info(data)
    elif data.startswith(b"\xff\xd8\xff"):
        info = _jpeg_info(data)
    elif data.startswith(b"RIFF") and len(data) >= 30 and data[8:12] == b"WEBP":
        info = _webp_info(data)
    else:
        raise ValueError("资源不是受支持的 PNG、JPEG 或 WebP 图片")
    if info.width <= 0 or info.height <= 0:
        raise ValueError("图片尺寸无效")
    if info.width > max_dimension or info.height > max_dimension:
        raise ValueError("图片尺寸超过安全限制")
    return info


def _png_info(data: bytes) -> ImageInfo:
    if (
        len(data) < 45
        or data[8:12] != b"\x00\x00\x00\r"
        or data[12:16] != b"IHDR"
        or data[-12:-8] != b"\x00\x00\x00\x00"
        or data[-8:-4] != b"IEND"
    ):
        raise ValueError("PNG 文件头不完整")
    width, height = struct.unpack_from(">II", data, 16)
    return ImageInfo("png", "image/png", width, height)


def _jpeg_info(data: bytes) -> ImageInfo:
    if not data.endswith(b"\xff\xd9"):
        raise ValueError("JPEG 文件尾不完整")
    offset = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        length = struct.unpack_from(">H", data, offset)[0]
        if length < 2 or offset + length > len(data):
            raise ValueError("JPEG 段长度无效")
        if marker in sof_markers:
            if length < 7:
                raise ValueError("JPEG 尺寸段不完整")
            height, width = struct.unpack_from(">HH", data, offset + 3)
            return ImageInfo("jpg", "image/jpeg", width, height)
        offset += length
    raise ValueError("无法读取 JPEG 尺寸")


def _webp_info(data: bytes) -> ImageInfo:
    declared_size = int.from_bytes(data[4:8], "little") + 8
    if declared_size != len(data):
        raise ValueError("WebP 文件长度无效")
    kind = data[12:16]
    if kind == b"VP8X":
        if len(data) < 30:
            raise ValueError("WebP 文件头不完整")
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
    elif kind == b"VP8L":
        if len(data) < 25 or data[20] != 0x2F:
            raise ValueError("WebP 无损文件头无效")
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    elif kind == b"VP8 ":
        if len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
            raise ValueError("WebP 有损文件头无效")
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
    else:
        raise ValueError("WebP 编码格式不受支持")
    return ImageInfo("webp", "image/webp", width, height)
