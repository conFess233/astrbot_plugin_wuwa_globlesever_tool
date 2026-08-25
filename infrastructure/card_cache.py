"""按档案作用域删除可再生成的图片卡缓存。"""

from pathlib import Path


def remove_profile_cards(directory: Path | None, profile_ids: tuple[int, ...]) -> int:
    if directory is None or not directory.is_dir():
        return 0
    root = directory.resolve()
    removed = 0
    for profile_id in profile_ids:
        for candidate in directory.glob(f"profile-{profile_id}-*.png"):
            if candidate.is_file() and candidate.resolve().parent == root:
                candidate.unlink(missing_ok=True)
                removed += 1
    return removed


def remove_all_cards(directory: Path | None) -> int:
    """删除旧渲染器生成的可再生 PNG，不递归触碰其他数据。"""

    if directory is None or not directory.is_dir():
        return 0
    root = directory.resolve()
    removed = 0
    for candidate in directory.glob("*.png"):
        if candidate.is_file() and candidate.resolve().parent == root:
            candidate.unlink(missing_ok=True)
            removed += 1
    return removed
