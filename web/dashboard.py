"""AstrBot Plugin Page 使用的管理员 Web API。"""

import asyncio
import contextlib
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    file_response,
    json_response,
    request,
)

from ..services.backups import BackupError, BackupService
from ..services.dashboard import DashboardError, DashboardService
from ..services.settings import SettingsError

DashboardGetter = Callable[[], DashboardService | None]
BackupGetter = Callable[[], BackupService | None]


class DashboardWebManager:
    def __init__(
        self,
        dashboard: DashboardGetter,
        backups: BackupGetter,
        backups_directory: Path,
    ):
        self._dashboard = dashboard
        self._backups = backups
        self.backups_directory = backups_directory
        self._pending_imports: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _authenticated() -> bool:
        return bool(request.username)

    def _services(self) -> tuple[DashboardService, BackupService]:
        dashboard = self._dashboard()
        backups = self._backups()
        if dashboard is None or backups is None:
            raise DashboardError("插件尚未完成初始化")
        return dashboard, backups

    async def overview(self):
        return await self._read(lambda dashboard: dashboard.overview())

    async def accounts(self):
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        try:
            dashboard, _ = self._services()
            return json_response(
                await dashboard.accounts(
                    query=str(request.query.get("q", "") or ""),
                    token_status=str(request.query.get("token_status", "") or ""),
                    sync_status=str(request.query.get("sync_status", "") or ""),
                    page=max(1, request.query.get("page", 1, type=int)),
                    page_size=request.query.get("page_size", 20, type=int),
                )
            )
        except DashboardError as exc:
            return error_response(str(exc))

    async def force_unbind(self):
        return await self._write(
            lambda dashboard, payload: dashboard.force_unbind(
                str(request.username),
                str(payload.get("region_id") or ""),
                str(payload.get("uid") or ""),
                str(payload.get("confirmation") or ""),
            )
        )

    async def delete_user(self):
        return await self._write(
            lambda dashboard, payload: dashboard.delete_user(
                str(request.username),
                str(payload.get("qq_id") or ""),
                str(payload.get("confirmation") or ""),
            )
        )

    async def get_config(self):
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        try:
            dashboard, _ = self._services()
            return json_response(dashboard.config_snapshot())
        except DashboardError as exc:
            return error_response(str(exc))

    async def save_config(self):
        return await self._write(
            lambda dashboard, payload: dashboard.save_config(str(request.username), payload)
        )

    async def resources(self):
        return await self._read(lambda dashboard: dashboard.resource_status())

    async def check_resources(self):
        return await self._write(lambda dashboard, _payload: dashboard.check_resources())

    async def update_resources(self):
        return await self._write(
            lambda dashboard, _payload: dashboard.update_resources(str(request.username))
        )

    async def rollback_resources(self):
        return await self._write(
            lambda dashboard, payload: dashboard.rollback_resources(
                str(request.username), str(payload.get("confirmation") or "")
            )
        )

    async def cleanup_cache(self):
        return await self._write(
            lambda dashboard, payload: dashboard.cleanup_cache(
                str(request.username), str(payload.get("confirmation") or "")
            )
        )

    async def fonts(self):
        return await self._read(lambda dashboard: dashboard.fonts_snapshot())

    async def install_font(self):
        return await self._write(
            lambda dashboard, payload: dashboard.install_font(
                str(request.username),
                str(payload.get("url") or ""),
                str(payload.get("display_name") or ""),
                bool(payload.get("make_default")),
            )
        )

    async def set_default_font(self):
        return await self._write(
            lambda dashboard, payload: dashboard.set_default_font(
                str(request.username), str(payload.get("font_id") or "")
            )
        )

    async def delete_font(self):
        return await self._write(
            lambda dashboard, payload: dashboard.delete_font(
                str(request.username),
                str(payload.get("font_id") or ""),
                str(payload.get("confirmation") or ""),
            )
        )

    async def card_preview(self):
        kind = str(request.query.get("kind", "account_info") or "account_info")
        return await self._read(lambda dashboard: dashboard.card_preview(kind))

    async def audit(self):
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        try:
            dashboard, _ = self._services()
            limit = min(1000, max(1, request.query.get("limit", 200, type=int)))
            return json_response({"items": await dashboard.audit(limit)})
        except DashboardError as exc:
            return error_response(str(exc))

    async def export_backup(self):
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        try:
            dashboard, backups = self._services()
            include = str(request.query.get("include_credentials", "false")).casefold() == "true"
            if include and request.query.get("confirmation") != "导出加密凭据":
                raise BackupError("请输入“导出加密凭据”进行确认")
            path = await backups.export(
                dashboard.config_snapshot(),
                include_encrypted_credentials=include,
            )
            await dashboard.record_audit(
                str(request.username),
                "backup_export",
                "encrypted" if include else "sanitized",
                "success",
            )
            return file_response(
                path,
                filename=path.name,
                content_type="application/zip",
            )
        except (BackupError, DashboardError, OSError) as exc:
            return error_response(str(exc))

    async def inspect_backup(self):
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("缺少备份 ZIP")
        if upload.content_length and upload.content_length > 128 * 1024 * 1024:
            await upload.close()
            return error_response("备份文件不能超过 128 MiB")
        temporary: Path | None = None
        try:
            self._prune_imports()
            await asyncio.to_thread(self.backups_directory.mkdir, parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(
                dir=self.backups_directory, prefix=".dashboard-import-", suffix=".zip"
            )
            os.close(fd)
            temporary = Path(name)
            await upload.save(temporary)
            _, backups = self._services()
            inspection = await backups.inspect(temporary)
            token = uuid.uuid4().hex
            self._pending_imports[token] = {
                "owner": request.username,
                "path": temporary,
                "expires_at": time.monotonic() + 600,
                "inspection": inspection,
            }
            temporary = None
            return json_response({"token": token, **inspection.to_dict()})
        except (BackupError, DashboardError, OSError) as exc:
            return error_response(str(exc))
        finally:
            await upload.close()
            if temporary is not None:
                await asyncio.to_thread(temporary.unlink, missing_ok=True)

    async def commit_backup(self):
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式无效")
        token = str(payload.get("token") or "")
        pending = self._pending_imports.pop(token, None)
        if (
            pending is None
            or pending["owner"] != request.username
            or pending["expires_at"] < time.monotonic()
        ):
            if pending is not None:
                await asyncio.to_thread(Path(pending["path"]).unlink, missing_ok=True)
            return error_response("备份确认已失效，请重新上传")
        archive = Path(pending["path"])
        try:
            if str(payload.get("confirmation") or "") != "确认恢复备份":
                raise BackupError("请输入“确认恢复备份”进行确认")
            dashboard, backups = self._services()
            safety_backup = await backups.export(
                dashboard.config_snapshot(), include_encrypted_credentials=True
            )
            result = await backups.restore(
                archive,
                mode=str(payload.get("mode") or "preserve"),
                restore_credentials=bool(payload.get("restore_credentials")),
            )
            if bool(payload.get("restore_catalog", True)):
                catalog_payload = await backups.catalog_payload(archive)
                await dashboard.install_catalog(
                    str(request.username),
                    catalog_payload,
                    action="backup_catalog_restore",
                )
            if bool(payload.get("restore_settings")):
                await dashboard.save_config(str(request.username), pending["inspection"].config)
            await dashboard.record_audit(
                str(request.username),
                "backup_restore",
                str(payload.get("mode") or "preserve"),
                "success",
            )
            return json_response({"result": result, "safety_backup": safety_backup.name})
        except (BackupError, DashboardError, SettingsError, OSError) as exc:
            return error_response(str(exc))
        finally:
            await asyncio.to_thread(archive.unlink, missing_ok=True)

    async def close(self) -> None:
        for pending in self._pending_imports.values():
            with contextlib.suppress(OSError):
                await asyncio.to_thread(Path(pending["path"]).unlink, missing_ok=True)
        self._pending_imports.clear()

    async def _read(self, operation):
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        try:
            dashboard, _ = self._services()
            return json_response(await operation(dashboard))
        except (DashboardError, OSError, ValueError) as exc:
            return error_response(str(exc))

    async def _write(self, operation):
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式无效")
        try:
            dashboard, _ = self._services()
            return json_response(await operation(dashboard, payload))
        except (DashboardError, SettingsError, OSError, ValueError) as exc:
            return error_response(str(exc))

    def _prune_imports(self) -> None:
        now = time.monotonic()
        for token, pending in tuple(self._pending_imports.items()):
            if pending["expires_at"] >= now:
                continue
            Path(pending["path"]).unlink(missing_ok=True)
            self._pending_imports.pop(token, None)
