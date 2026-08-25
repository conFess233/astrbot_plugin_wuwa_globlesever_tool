"""AstrBot Dashboard 内受 API Key 保护的插件健康接口。"""

from collections.abc import Callable
from typing import Any

from astrbot.api.web import error_response, json_response, request

from ...constants import PLUGIN_NAME, PLUGIN_VERSION
from ...infrastructure.database import Database
from ...infrastructure.network import HttpClient
from ...infrastructure.storage import RuntimePaths


class WebManager:
    def __init__(
        self,
        paths: RuntimePaths,
        database: Database,
        http: HttpClient,
        initialized: Callable[[], bool],
    ):
        self.paths = paths
        self.database = database
        self.http = http
        self.initialized = initialized

    @staticmethod
    def _authenticated() -> bool:
        return bool(request.username)

    async def health(self):
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        database = await self.database.health()
        payload: dict[str, Any] = {
            "plugin": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "initialized": self.initialized(),
            "database": database,
            "http": self.http.status(),
            "storage_root": str(self.paths.root),
        }
        return json_response(payload)
