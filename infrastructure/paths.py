"""解析并创建 AstrBot 标准插件数据目录。"""

from dataclasses import dataclass
from pathlib import Path

from ..constants import PLUGIN_NAME


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    database: Path
    secrets: Path
    cache_character: Path
    cache_weapon: Path
    cache_static_data: Path
    media_cards: Path
    media_temp: Path
    migrations: Path
    backups: Path
    logs: Path

    @classmethod
    def from_plugin_data_root(cls, plugin_data_root: str | Path) -> "RuntimePaths":
        root = Path(plugin_data_root).resolve() / PLUGIN_NAME
        return cls(
            root=root,
            database=root / "wuwa.sqlite3",
            secrets=root / "secrets",
            cache_character=root / "cache" / "character",
            cache_weapon=root / "cache" / "weapon",
            cache_static_data=root / "cache" / "static_data",
            media_cards=root / "media" / "cards",
            media_temp=root / "media" / "temp",
            migrations=root / "migrations",
            backups=root / "backups",
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
            self.cache_character,
            self.cache_weapon,
            self.cache_static_data,
            self.media_cards,
            self.media_temp,
            self.migrations,
            self.backups,
            self.logs,
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
