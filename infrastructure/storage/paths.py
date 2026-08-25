"""解析并创建 AstrBot 标准插件数据目录。"""

from dataclasses import dataclass
from pathlib import Path

from ...constants import PLUGIN_NAME


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    database: Path
    secrets: Path
    settings: Path
    snapshots_raw: Path
    cache_resources: Path
    cache_character: Path
    cache_weapon: Path
    cache_static_data: Path
    cache_manifests: Path
    media_cards: Path
    media_temp: Path
    fonts: Path
    migrations: Path
    backups: Path
    exports: Path
    logs: Path

    @classmethod
    def from_plugin_data_root(cls, plugin_data_root: str | Path) -> "RuntimePaths":
        root = Path(plugin_data_root).resolve() / PLUGIN_NAME
        cache = root / "cache"
        media = root / "media"
        return cls(
            root=root,
            # 保留历史文件名，避免升级时移动带 WAL 的活动数据库。
            database=root / "wuwa.sqlite3",
            secrets=root / "secrets",
            settings=root / "settings",
            snapshots_raw=root / "snapshots" / "raw",
            cache_resources=cache / "resources",
            cache_character=cache / "character",
            cache_weapon=cache / "weapon",
            cache_static_data=cache / "static_data",
            cache_manifests=cache / "manifests",
            media_cards=media / "cards",
            media_temp=media / "temp",
            fonts=root / "fonts",
            migrations=root / "migrations",
            backups=root / "backups",
            exports=root / "exports",
            logs=root / "logs",
        )

    @classmethod
    def from_astrbot(cls) -> "RuntimePaths":
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return cls.from_plugin_data_root(get_astrbot_plugin_data_path())

    def ensure(self) -> None:
        directories = (
            self.root,
            self.secrets,
            self.settings,
            self.snapshots_raw,
            self.cache_resources,
            self.cache_character,
            self.cache_weapon,
            self.cache_static_data,
            self.cache_manifests,
            self.media_cards,
            self.media_temp,
            self.fonts,
            self.migrations,
            self.backups,
            self.exports,
            self.logs,
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
