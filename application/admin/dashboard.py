"""Dashboard 管理核心：统计、账号、配置、资源与审计。"""

import asyncio
import base64
import json
import shutil
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ...constants import PLUGIN_VERSION, SCHEMA_VERSION
from ...domain.catalog import CharacterCatalog
from ...infrastructure.database import Database
from ...infrastructure.security import TokenCipher
from ...infrastructure.storage import RuntimePaths, remove_all_cards, remove_profile_cards
from ...presentation.cards import CardPreviewService
from ...presentation.resources import FontEntry, FontManager, ResourceManager
from ..settings import PluginSettings

_CATALOG_URL = "https://guide-server.aki-game.net/role/avatar/list"
_MAX_CATALOG_BYTES = 4 * 1024 * 1024
_EDITABLE_CONFIG_KEYS = tuple(PluginSettings.__dataclass_fields__)

PersistConfig = Callable[[], Awaitable[None]]
ApplySettings = Callable[[PluginSettings], Awaitable[None]]
ApplyCatalog = Callable[[CharacterCatalog], None]
FetchCatalog = Callable[[], Awaitable[object]]
AutoSyncState = Callable[[], bool]


class DashboardError(ValueError):
    """表示后台管理请求未通过服务端校验。"""


class DashboardService:
    def __init__(
        self,
        database: Database,
        paths: RuntimePaths,
        cipher: TokenCipher,
        config: Mapping[str, Any],
        settings: PluginSettings,
        persist_config: PersistConfig,
        apply_settings: ApplySettings,
        apply_catalog: ApplyCatalog,
        fetch_catalog: FetchCatalog,
        auto_sync_running: AutoSyncState | None = None,
        resource_manager: ResourceManager | None = None,
        font_manager: FontManager | None = None,
        card_previews: CardPreviewService | None = None,
    ):
        self.database = database
        self.paths = paths
        self.cipher = cipher
        self.config = config
        self.settings = settings
        self.persist_config = persist_config
        self.apply_settings = apply_settings
        self.apply_catalog = apply_catalog
        self.fetch_catalog = fetch_catalog
        self.auto_sync_running = auto_sync_running or (lambda: False)
        self.resource_manager = resource_manager
        self.font_manager = font_manager
        self.card_previews = card_previews
        self.bundled_catalog = (
            Path(__file__).resolve().parent.parent.parent / "static" / "data" / "characters.json"
        )
        self.font_presets = (
            Path(__file__).resolve().parent.parent.parent / "static" / "fonts" / "presets.json"
        )

    async def overview(self) -> dict[str, object]:
        counts = await self.database.read(self._overview_rows)
        resources = await self.resource_status()
        return {
            "version": PLUGIN_VERSION,
            "schema_version": SCHEMA_VERSION,
            **counts,
            "resources": resources,
            "auto_sync": {
                "enabled": self.settings.auto_sync_enabled,
                "running": self.auto_sync_running(),
                "interval_hours": self.settings.auto_sync_interval_hours,
            },
        }

    async def accounts(
        self,
        *,
        query: str = "",
        token_status: str = "",
        sync_status: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, object]:
        page = max(1, page)
        page_size = min(100, max(1, page_size))
        token_status = token_status.strip()
        sync_status = sync_status.strip()
        needle = query.strip()

        def operation(db: sqlite3.Connection) -> dict[str, object]:
            clauses = []
            parameters: list[object] = []
            if needle:
                clauses.append(
                    "(u.qq_id LIKE ? OR c.email_masked LIKE ? OR g.uid LIKE ? "
                    "OR g.region_name LIKE ?)"
                )
                pattern = f"%{needle}%"
                parameters.extend((pattern, pattern, pattern, pattern))
            if token_status:
                clauses.append("(c.game_token_status = ? OR c.guide_token_status = ?)")
                parameters.extend((token_status, token_status))
            if sync_status:
                clauses.append("g.sync_status = ?")
                parameters.append(sync_status)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            base = (
                " FROM game_accounts g JOIN users u ON u.qq_id = g.qq_id "
                "JOIN credentials c ON c.credential_id = g.credential_id "
            )
            total = int(db.execute(f"SELECT COUNT(*){base}{where}", parameters).fetchone()[0])
            rows = db.execute(
                "SELECT u.qq_id, u.default_region_id, u.default_uid, "
                "c.email_masked, c.game_token_status, c.guide_token_status, "
                "g.uid, g.region_id, g.region_name, g.player_name, g.sync_status, "
                "g.last_sync_attempt_at, g.last_sync_success_at, g.last_error_category "
                f"{base}{where} ORDER BY u.qq_id, g.region_id, g.uid LIMIT ? OFFSET ?",
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
            return {
                "items": [
                    {
                        **dict(row),
                        "is_default": (
                            str(row["default_region_id"] or "") == str(row["region_id"])
                            and str(row["default_uid"] or "") == str(row["uid"])
                        ),
                    }
                    for row in rows
                ],
                "total": total,
                "page": page,
                "page_size": page_size,
            }

        return await self.database.read(operation)

    async def force_unbind(
        self,
        admin: str,
        region_id: str,
        uid: str,
        confirmation: str,
    ) -> dict[str, object]:
        region_id = region_id.strip()
        uid = uid.strip()
        account_key = f"{region_id}/{uid}"
        if not region_id or not uid or confirmation.strip() != account_key:
            raise DashboardError("必须完整输入“区服/UID”进行确认")

        def operation(db: sqlite3.Connection) -> dict[str, object]:
            row = db.execute(
                "SELECT g.qq_id, g.credential_id, p.profile_id "
                "FROM game_accounts g LEFT JOIN profiles p ON p.qq_id = g.qq_id "
                "AND p.region_id = g.region_id AND p.uid = g.uid "
                "WHERE g.region_id = ? AND g.uid = ? LIMIT 1",
                (region_id, uid),
            ).fetchone()
            if row is None:
                raise DashboardError("指定区服中的 UID 不存在")
            qq_id = str(row["qq_id"])
            credential_id = int(row["credential_id"])
            db.execute(
                "DELETE FROM game_accounts WHERE region_id = ? AND uid = ?",
                (region_id, uid),
            )
            replacement = db.execute(
                "SELECT region_id, uid FROM game_accounts WHERE qq_id = ? "
                "ORDER BY region_id, uid LIMIT 1",
                (qq_id,),
            ).fetchone()
            replacement_uid = str(replacement["uid"]) if replacement else None
            replacement_region_id = str(replacement["region_id"]) if replacement else None
            profile = db.execute(
                "SELECT profile_id FROM profiles WHERE qq_id = ? AND "
                "((? IS NULL AND profile_type = 'local') "
                "OR (region_id = ? AND uid = ?)) LIMIT 1",
                (
                    qq_id,
                    replacement_uid,
                    replacement_region_id,
                    replacement_uid,
                ),
            ).fetchone()
            db.execute(
                "UPDATE users SET default_region_id = ?, default_uid = ?, "
                "active_profile_id = ?, updated_at = ? "
                "WHERE qq_id = ?",
                (
                    replacement_region_id,
                    replacement_uid,
                    int(profile["profile_id"]) if profile else None,
                    _iso(),
                    qq_id,
                ),
            )
            used = db.execute(
                "SELECT 1 FROM game_accounts WHERE credential_id = ? LIMIT 1",
                (credential_id,),
            ).fetchone()
            if used is None:
                db.execute("DELETE FROM credentials WHERE credential_id = ?", (credential_id,))
            self._audit(db, admin, "force_unbind", account_key, "success")
            return {
                "uid": uid,
                "region_id": region_id,
                "qq_id": qq_id,
                "default_region_id": replacement_region_id,
                "default_uid": replacement_uid,
                "_profile_ids": [int(row["profile_id"])] if row["profile_id"] else [],
            }

        result = await self.database.write(operation)
        await asyncio.to_thread(
            remove_profile_cards,
            self.paths.media_cards,
            tuple(result.pop("_profile_ids")),
        )
        return result

    async def delete_user(self, admin: str, qq_id: str, confirmation: str) -> dict[str, object]:
        qq_id = _numeric_id(qq_id, "QQ 号")
        if confirmation.strip() != qq_id:
            raise DashboardError("必须完整输入要删除的 QQ 号进行确认")

        def operation(db: sqlite3.Connection) -> dict[str, object]:
            profile_ids = tuple(
                int(row["profile_id"])
                for row in db.execute(
                    "SELECT profile_id FROM profiles WHERE qq_id = ?", (qq_id,)
                ).fetchall()
            )
            counts = db.execute(
                "SELECT (SELECT COUNT(*) FROM game_accounts WHERE qq_id = ?) AS accounts, "
                "(SELECT COUNT(*) FROM characters c JOIN profiles p "
                "ON p.profile_id = c.profile_id WHERE p.qq_id = ?) AS characters",
                (qq_id, qq_id),
            ).fetchone()
            deleted = db.execute("DELETE FROM users WHERE qq_id = ?", (qq_id,)).rowcount
            if not deleted:
                raise DashboardError("QQ 用户不存在")
            self._audit(db, admin, "delete_user", _mask_qq(qq_id), "success")
            return {
                "qq_id": qq_id,
                "accounts": int(counts["accounts"]),
                "characters": int(counts["characters"]),
                "_profile_ids": profile_ids,
            }

        result = await self.database.write(operation)
        await asyncio.to_thread(
            remove_profile_cards,
            self.paths.media_cards,
            tuple(result.pop("_profile_ids")),
        )
        return result

    def config_snapshot(self) -> dict[str, object]:
        return {key: _json_value(getattr(self.settings, key)) for key in _EDITABLE_CONFIG_KEYS}

    async def save_config(self, admin: str, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise DashboardError("配置必须是对象")
        unknown = set(payload) - set(_EDITABLE_CONFIG_KEYS)
        if unknown:
            raise DashboardError(f"包含未知配置：{'、'.join(sorted(unknown))}")
        candidate = {**dict(self.config), **payload}
        validated = PluginSettings.from_mapping(candidate)
        mutable = self.config
        if not hasattr(mutable, "update"):
            raise DashboardError("当前 AstrBot 配置对象不可写")
        previous = self.settings
        previous_values = {
            key: _json_value(getattr(previous, key)) for key in _EDITABLE_CONFIG_KEYS
        }
        await self.apply_settings(validated)
        try:
            mutable.update(
                {key: _json_value(getattr(validated, key)) for key in _EDITABLE_CONFIG_KEYS}
            )
            await self.persist_config()
        except Exception:
            mutable.update(previous_values)
            await self.apply_settings(previous)
            raise
        self.settings = validated
        await self.database.write(
            lambda db: self._audit(db, admin, "save_config", "global", "success")
        )
        return self.config_snapshot()

    async def resource_status(self) -> dict[str, object]:
        override = self.paths.cache_static_data / "characters.json"
        selected = override if override.is_file() else self.bundled_catalog
        payload = await asyncio.to_thread(_read_json, selected)
        managed = (
            await self.resource_manager.status()
            if self.resource_manager is not None
            else {"count": 0, "bytes": 0, "referenced": 0, "limit_bytes": 0}
        )
        fonts = await self.font_manager.list_fonts() if self.font_manager is not None else ()
        default_font = next((item for item in fonts if item.is_default), None)
        return {
            "source": "runtime" if selected == override else "bundled",
            "schema_version": payload.get("schema_version"),
            "snapshot_date": payload.get("snapshot_date"),
            "character_count": len(payload.get("characters", [])),
            "character_cache": await asyncio.to_thread(
                _directory_stats, self.paths.cache_character
            ),
            "card_cache": await asyncio.to_thread(_directory_stats, self.paths.media_cards),
            "temp": await asyncio.to_thread(_directory_stats, self.paths.media_temp),
            "managed_images": managed,
            "fonts": {
                "count": len(fonts),
                "default": default_font.display_name if default_font is not None else None,
            },
            "can_rollback": (self.paths.cache_static_data / "characters.previous.json").is_file()
            or override.is_file(),
        }

    async def check_resources(self) -> dict[str, object]:
        remote = _catalog_snapshot(await self.fetch_catalog())
        current_path = self.paths.cache_static_data / "characters.json"
        if not current_path.is_file():
            current_path = self.bundled_catalog
        current = await asyncio.to_thread(_read_json, current_path)
        return {
            "update_available": _catalog_digest(remote) != _catalog_digest(current),
            "current_count": len(current.get("characters", [])),
            "remote_count": len(remote["characters"]),
            "remote_snapshot_date": remote["snapshot_date"],
        }

    async def update_resources(self, admin: str) -> dict[str, object]:
        payload = _catalog_snapshot(await self.fetch_catalog())
        return await self.install_catalog(admin, payload, action="resource_update")

    async def install_catalog(
        self,
        admin: str,
        payload: dict[str, object],
        *,
        action: str,
    ) -> dict[str, object]:
        catalog = CharacterCatalog.from_payload(payload)
        target = self.paths.cache_static_data / "characters.json"
        previous = self.paths.cache_static_data / "characters.previous.json"
        await asyncio.to_thread(self.paths.cache_static_data.mkdir, parents=True, exist_ok=True)
        if target.is_file():
            await asyncio.to_thread(shutil.copyfile, target, previous)
        await asyncio.to_thread(_write_json_atomic, target, payload)
        self.apply_catalog(catalog)
        await self.database.write(
            lambda db: self._audit(
                db,
                admin,
                action,
                f"characters:{len(payload['characters'])}",
                "success",
            )
        )
        return await self.resource_status()

    async def rollback_resources(self, admin: str, confirmation: str) -> dict[str, object]:
        if confirmation.strip() != "回滚角色资源":
            raise DashboardError("请输入“回滚角色资源”进行确认")
        target = self.paths.cache_static_data / "characters.json"
        previous = self.paths.cache_static_data / "characters.previous.json"
        if previous.is_file():
            await asyncio.to_thread(shutil.copyfile, previous, target)
            await asyncio.to_thread(previous.unlink, missing_ok=True)
        elif target.is_file():
            await asyncio.to_thread(target.unlink, missing_ok=True)
        else:
            raise DashboardError("没有可回滚的角色资源")
        catalog = CharacterCatalog.load_bundled(target)
        self.apply_catalog(catalog)
        await self.database.write(
            lambda db: self._audit(db, admin, "resource_rollback", "characters", "success")
        )
        return await self.resource_status()

    async def cleanup_cache(self, admin: str, confirmation: str) -> dict[str, object]:
        if confirmation.strip() != "清理缓存":
            raise DashboardError("请输入“清理缓存”进行确认")
        removed = await asyncio.to_thread(
            _clear_files, (self.paths.media_temp, self.paths.media_cards)
        )
        managed = (
            await self.resource_manager.cleanup()
            if self.resource_manager is not None
            else {"removed_files": 0, "removed_bytes": 0, "remaining_bytes": 0}
        )
        await self.database.write(
            lambda db: self._audit(
                db, admin, "cleanup_cache", "render-and-temp", f"removed:{removed}"
            )
        )
        return {
            "removed": removed + managed["removed_files"],
            "managed": managed,
            "status": await self.resource_status(),
        }

    async def fonts_snapshot(self) -> dict[str, object]:
        if self.font_manager is None:
            raise DashboardError("字体管理服务尚未初始化")
        presets = await asyncio.to_thread(_read_json, self.font_presets)
        fonts = await self.font_manager.list_fonts()
        return {
            "presets": presets.get("presets", []),
            "items": [_font_payload(item) for item in fonts],
            "default_font_id": next((item.font_id for item in fonts if item.is_default), None),
        }

    async def install_font(
        self,
        admin: str,
        url: str,
        display_name: str,
        make_default: bool,
    ) -> dict[str, object]:
        if self.font_manager is None:
            raise DashboardError("字体管理服务尚未初始化")
        url = url.strip()
        display_name = display_name.strip()
        if not url or len(url) > 2048:
            raise DashboardError("字体下载地址无效")
        if len(display_name) > 120:
            raise DashboardError("字体显示名称过长")
        installed = await self.font_manager.install_from_url(
            url,
            display_name=display_name or None,
            make_default=make_default,
        )
        await self.database.write(
            lambda db: self._audit(
                db,
                admin,
                "font_install",
                f"fonts:{len(installed)}",
                "success",
            )
        )
        return await self.fonts_snapshot()

    async def set_default_font(self, admin: str, font_id: str) -> dict[str, object]:
        if self.font_manager is None:
            raise DashboardError("字体管理服务尚未初始化")
        selected = await self.font_manager.set_default(font_id.strip())
        await self.database.write(
            lambda db: self._audit(db, admin, "font_default", selected.display_name, "success")
        )
        await asyncio.to_thread(remove_all_cards, self.paths.media_cards)
        return await self.fonts_snapshot()

    async def delete_font(self, admin: str, font_id: str, confirmation: str) -> dict[str, object]:
        if self.font_manager is None:
            raise DashboardError("字体管理服务尚未初始化")
        if confirmation.strip() != "删除字体":
            raise DashboardError("请输入“删除字体”进行确认")
        await self.font_manager.delete(font_id.strip())
        await self.database.write(
            lambda db: self._audit(db, admin, "font_delete", font_id[:12], "success")
        )
        return await self.fonts_snapshot()

    async def card_preview(self, kind: str) -> dict[str, object]:
        if self.card_previews is None:
            raise DashboardError("卡片预览服务尚未初始化")
        path = await self.card_previews.render(kind.strip())
        data = await asyncio.to_thread(path.read_bytes)
        if len(data) > 32 * 1024 * 1024:
            raise DashboardError("卡片预览文件过大")
        return {
            "kind": kind,
            "data_uri": f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}",
            "bytes": len(data),
            "kinds": self.card_previews.kinds,
        }

    async def audit(self, limit: int = 200) -> list[dict[str, object]]:
        limit = min(1000, max(1, limit))
        return await self.database.read(
            lambda db: [
                dict(row)
                for row in db.execute(
                    "SELECT audit_id, admin_identity, action_type, masked_target, result, "
                    "created_at FROM admin_audit ORDER BY audit_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
        )

    async def record_audit(self, admin: str, action: str, target: str, result: str) -> None:
        await self.database.write(lambda db: self._audit(db, admin, action, target, result))

    @staticmethod
    def _overview_rows(db: sqlite3.Connection) -> dict[str, object]:
        row = db.execute(
            "SELECT (SELECT COUNT(*) FROM users) AS users, "
            "(SELECT COUNT(*) FROM credentials) AS credentials, "
            "(SELECT COUNT(*) FROM game_accounts) AS accounts, "
            "(SELECT COUNT(*) FROM characters) AS characters, "
            "(SELECT COUNT(*) FROM credentials WHERE game_token_status = 'valid') "
            "AS game_token_valid, "
            "(SELECT COUNT(*) FROM credentials WHERE guide_token_status = 'valid') "
            "AS guide_token_valid, "
            "(SELECT COUNT(*) FROM credentials WHERE game_token_status = 'needs_login') "
            "AS game_needs_login, "
            "(SELECT COUNT(*) FROM credentials WHERE guide_token_status = 'needs_login') "
            "AS guide_needs_login, "
            "(SELECT COUNT(*) FROM game_accounts WHERE sync_status = 'failed') AS sync_failed, "
            "(SELECT MAX(last_sync_success_at) FROM game_accounts) AS last_global_sync"
        ).fetchone()
        return dict(row)

    def _audit(
        self,
        db: sqlite3.Connection,
        admin: str,
        action: str,
        target: str,
        result: str,
    ) -> None:
        if self.settings.admin_audit_retention_days == 0:
            return
        db.execute(
            "INSERT INTO admin_audit (admin_identity, action_type, masked_target, result, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (admin, action, target, result, _iso()),
        )
        threshold = _iso(
            datetime.now(UTC) - timedelta(days=self.settings.admin_audit_retention_days)
        )
        db.execute("DELETE FROM admin_audit WHERE created_at < ?", (threshold,))


def _catalog_snapshot(payload: object) -> dict[str, object]:
    if (
        not isinstance(payload, dict)
        or str(payload.get("code") or "").strip() != "200"
        or not isinstance(payload.get("data"), list)
    ):
        raise DashboardError("攻略站角色目录格式无效")
    characters = []
    languages = {"zh-Hans": "zh-CN", "zh-Hant": "zh-TW", "en": "en", "ja": "ja", "ko": "ko"}
    for role in payload["data"]:
        if not isinstance(role, dict):
            continue
        role_id = str(role.get("roleGbId") or "").strip()
        if not role_id:
            continue
        item: dict[str, object] = {"id": role_id}
        for text in role.get("texts") or []:
            if not isinstance(text, dict):
                continue
            key = languages.get(str(text.get("language") or ""))
            name = str(text.get("name") or "").strip()
            if key and name:
                item[key] = name
        element = role.get("element") if isinstance(role.get("element"), dict) else {}
        item.update(
            {
                "card_picture_url": str(role.get("cardPictureUrl") or ""),
                "illustration_picture_url": str(role.get("illustrationPictureUrl") or ""),
                "star": role.get("star"),
                "element_id": str(element.get("gbId") or ""),
                "element_picture_url": str(element.get("pictureUrl") or ""),
            }
        )
        characters.append(item)
    result: dict[str, object] = {
        "schema_version": 2,
        "source": "Wuthering Waves Guide role avatar list",
        "source_url": _CATALOG_URL,
        "snapshot_date": datetime.now(UTC).date().isoformat(),
        "characters": characters,
    }
    CharacterCatalog.from_payload(result)
    return result


def _catalog_digest(payload: dict[str, object]) -> str:
    characters = payload.get("characters") or []
    return json.dumps(characters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _directory_stats(path: Path) -> dict[str, int]:
    count = 0
    size = 0
    if path.is_dir():
        for item in path.rglob("*"):
            if item.is_file():
                count += 1
                size += item.stat().st_size
    return {"count": count, "bytes": size}


def _clear_files(directories: tuple[Path, ...]) -> int:
    removed = 0
    for directory in directories:
        if not directory.is_dir():
            continue
        for item in directory.rglob("*"):
            if item.is_file() and item.resolve().is_relative_to(directory.resolve()):
                item.unlink(missing_ok=True)
                removed += 1
    return removed


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _numeric_id(value: object, label: str) -> str:
    result = str(value or "").strip()
    if not result.isdigit() or len(result) > 32:
        raise DashboardError(f"{label}必须是 1-32 位数字")
    return result


def _mask_qq(value: str) -> str:
    return value if len(value) <= 4 else f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def _font_payload(item: FontEntry) -> dict[str, object]:
    return {
        "font_id": item.font_id,
        "display_name": item.display_name,
        "source_url": item.source_url,
        "weight": item.weight,
        "style": item.style,
        "is_default": item.is_default,
        "installed_at": item.installed_at,
    }


def _json_value(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()
