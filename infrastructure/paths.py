"""旧导入路径兼容层；新代码使用 infrastructure.storage.paths。"""

from .storage.paths import RuntimePaths

__all__ = ["RuntimePaths"]
