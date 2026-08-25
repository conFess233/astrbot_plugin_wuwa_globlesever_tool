"""在 Pillow 可用时保守裁去 HTML 截图底部的纯色空白。"""

from __future__ import annotations

import os
from pathlib import Path


def trim_card_canvas(path: Path, *, padding: int = 20) -> bool:
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return False
    temporary = path.with_name(f".{path.name}.trimmed")
    try:
        with Image.open(path) as source:
            image = source.convert("RGBA")
            image.load()
        width, height = image.size
        if width < 2 or height < 2:
            return False
        comparison = image.convert("RGB")
        background = Image.new("RGB", comparison.size, comparison.getpixel((width - 1, height - 1)))
        bounds = ImageChops.difference(comparison, background).getbbox()
        if not bounds:
            return False
        left, top, right, bottom = bounds
        if left > padding + 4 or top > padding + 4:
            return False
        target_width = min(width, right + padding)
        target_height = min(height, bottom + padding)
        if target_width == width and target_height == height:
            return False
        image.crop((0, 0, target_width, target_height)).save(temporary, format="PNG")
        os.replace(temporary, path)
        return True
    except (OSError, ValueError):
        return False
    finally:
        temporary.unlink(missing_ok=True)
