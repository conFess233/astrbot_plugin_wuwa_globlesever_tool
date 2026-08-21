"""AstrBot 插件入口，仅负责注册、生命周期和依赖装配。"""

from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from .clients.auth import GlobalAuthClient
from .clients.guide import GlobalGuideClient
from .commands.event_adapter import mentioned_users, plain_text
from .commands.parser import CommandParseError, CommandParser
from .constants import PLUGIN_DISPLAY_NAME, PLUGIN_NAME, PLUGIN_VERSION
from .domain.cards import CardMessage
from .domain.login import LoginLinkMessage
from .domain.player import PlayerDataError
from .infrastructure.crypto import MasterKeyProvider, TokenCipher
from .infrastructure.database import Database
from .infrastructure.http import HttpClient
from .infrastructure.paths import RuntimePaths
from .repositories.accounts import AccountError, AccountRepository
from .repositories.local_data import LocalDataError, LocalDataRepository
from .services.backups import BackupService
from .services.cards import AstrBotCardRenderer, CardService
from .services.catalog import CatalogError, CharacterCatalog
from .services.command_service import CommandService, CommandServiceError
from .services.dashboard import DashboardService
from .services.login_sessions import LoginSessionError, LoginSessionService
from .services.player_data import PlayerDataService
from .services.resource_cache import StaticImageCache
from .services.settings import PluginSettings
from .services.sync import GuideSyncService, SyncError
from .web.dashboard import DashboardWebManager
from .web.manager import WebManager
from .web.public_login import PublicLoginServer

_HANDLED_EVENT_KEY = "wuwa_global_server_tool_handled"


@register(PLUGIN_NAME, "conFess233", PLUGIN_DISPLAY_NAME, PLUGIN_VERSION)
class WuWaGlobalServerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.plugin_root = Path(__file__).resolve().parent
        self.config = config
        self.settings = PluginSettings.from_mapping(dict(config))
        self.paths = RuntimePaths.from_astrbot()
        self.database = Database(self.paths.database)
        self.http = HttpClient(self.settings.request_timeout_seconds)
        self.cipher: TokenCipher | None = None
        self.catalog = CharacterCatalog.load_bundled(
            self.paths.cache_static_data / "characters.json"
        )
        self.parser = CommandParser(self.settings)
        self.repository: LocalDataRepository | None = None
        self.accounts: AccountRepository | None = None
        self.login_sessions: LoginSessionService | None = None
        self.sync_service: GuideSyncService | None = None
        self.player_data: PlayerDataService | None = None
        self.card_renderer: AstrBotCardRenderer | None = None
        self.commands: CommandService | None = None
        self.dashboard_service: DashboardService | None = None
        self.backup_service: BackupService | None = None
        self._initialized = False
        self.web = WebManager(
            self.paths,
            self.database,
            self.http,
            lambda: self._initialized,
            self.settings,
            lambda: self.login_sessions,
        )
        self.public_login = PublicLoginServer(self.settings, lambda: self.login_sessions)
        self.dashboard_web = DashboardWebManager(
            lambda: self.dashboard_service,
            lambda: self.backup_service,
            self.paths.backups,
        )
        self._register_web_routes()

    def _register_web_routes(self) -> None:
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/health",
            self.web.health,
            ["GET"],
            "鸣潮国际服数据工具运行状态",
        )
        dashboard_routes = (
            ("dashboard/overview", self.dashboard_web.overview, ["GET"], "后台运行概览"),
            ("dashboard/accounts", self.dashboard_web.accounts, ["GET"], "后台账号列表"),
            (
                "dashboard/accounts/unbind",
                self.dashboard_web.force_unbind,
                ["POST"],
                "强制解绑 UID",
            ),
            (
                "dashboard/users/delete",
                self.dashboard_web.delete_user,
                ["POST"],
                "删除 QQ 全部数据",
            ),
            ("dashboard/config", self.dashboard_web.get_config, ["GET"], "读取插件配置"),
            (
                "dashboard/config/save",
                self.dashboard_web.save_config,
                ["POST"],
                "保存插件配置",
            ),
            ("dashboard/resources", self.dashboard_web.resources, ["GET"], "读取资源状态"),
            (
                "dashboard/resources/check",
                self.dashboard_web.check_resources,
                ["POST"],
                "检查资源更新",
            ),
            (
                "dashboard/resources/update",
                self.dashboard_web.update_resources,
                ["POST"],
                "更新角色资源",
            ),
            (
                "dashboard/resources/rollback",
                self.dashboard_web.rollback_resources,
                ["POST"],
                "回滚角色资源",
            ),
            ("dashboard/cache/cleanup", self.dashboard_web.cleanup_cache, ["POST"], "清理渲染缓存"),
            ("dashboard/audit", self.dashboard_web.audit, ["GET"], "读取管理员审计"),
            ("dashboard/backup/export", self.dashboard_web.export_backup, ["GET"], "导出插件备份"),
            (
                "dashboard/backup/inspect",
                self.dashboard_web.inspect_backup,
                ["POST"],
                "预检插件备份",
            ),
            ("dashboard/backup/commit", self.dashboard_web.commit_backup, ["POST"], "恢复插件备份"),
        )
        for route, handler, methods, description in dashboard_routes:
            self.context.register_web_api(f"/{PLUGIN_NAME}/{route}", handler, methods, description)

    async def initialize(self) -> None:
        self.paths.ensure()
        master_key = MasterKeyProvider(self.paths.secrets).load_or_create()
        self.cipher = TokenCipher(master_key)
        await self.database.initialize()
        await self.http.initialize()
        self.repository = LocalDataRepository(
            self.database,
            self.cipher,
            self.settings.confirm_ttl_minutes,
            self.paths.media_cards,
        )
        self.accounts = AccountRepository(
            self.database,
            self.cipher,
            self.settings.confirm_ttl_minutes,
            self.paths.media_cards,
        )
        self.login_sessions = LoginSessionService(
            self.database,
            self.cipher,
            GlobalAuthClient(self.http),
            self.settings,
        )
        self.sync_service = GuideSyncService(
            self.database,
            self.cipher,
            GlobalGuideClient(self.http),
            self.catalog,
            self.settings,
        )
        self.player_data = PlayerDataService(self.database, self.cipher, self.http)
        self.card_renderer = AstrBotCardRenderer(
            self.plugin_root / "assets" / "templates" / "wuwa_cards.html",
            self.paths.media_cards,
            self._html_render_file,
            self.settings.render_timeout_seconds,
            StaticImageCache(self.paths.cache_character, self._fetch_static_image),
            StaticImageCache(self.paths.cache_weapon, self._fetch_static_image),
        )
        self.commands = CommandService(
            self.repository,
            self.catalog,
            self.settings,
            self.login_sessions,
            self.accounts,
            self.sync_service,
            self.player_data,
            CardService(self.catalog, self.card_renderer),
        )
        self.backup_service = BackupService(self.database, self.paths, self.cipher)
        self.dashboard_service = DashboardService(
            self.database,
            self.paths,
            self.cipher,
            self.config,
            self.settings,
            self._persist_config,
            self._apply_settings,
            self._apply_catalog,
            self._fetch_catalog,
            lambda: bool(self.sync_service is not None and self.sync_service.auto_sync_running),
        )
        await self.public_login.start()
        if self.public_login.running:
            logger.info(
                "%s：独立登录服务已监听 http://%s:%s",
                PLUGIN_DISPLAY_NAME,
                self.settings.login_server_host,
                self.settings.login_server_port,
            )
        self.sync_service.start_auto_sync()
        self._initialized = True
        logger.info("%s：本地档案与国际服登录服务初始化完成", PLUGIN_DISPLAY_NAME)

    @filter.command("kh")
    async def root_command(self, event: AstrMessageEvent, argument: str = ""):
        """处理永久兼容入口 /kh。"""
        if event.get_extra(_HANDLED_EVENT_KEY, False):
            return
        text = plain_text(event)
        if text.casefold() != "/kh" and not text.casefold().startswith("/kh "):
            text = f"/kh {argument}".strip()
        await self._dispatch(event, text)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_listener(self, event: AstrMessageEvent):
        """处理关键词、额外命令根和带前后置 At 的完整消息。"""
        if event.get_extra(_HANDLED_EVENT_KEY, False) or self._is_bot_message(event):
            return
        text = plain_text(event)
        try:
            command = self.parser.parse(text, mentioned_users(event))
        except CommandParseError as exc:
            event.set_extra(_HANDLED_EVENT_KEY, True)
            event.stop_event()
            await self._send(event, str(exc))
            return
        if command is None:
            return
        await self._dispatch(event, text, command=command)

    async def _dispatch(self, event: AstrMessageEvent, text: str, *, command=None) -> None:
        event.set_extra(_HANDLED_EVENT_KEY, True)
        event.stop_event()
        event.should_call_llm(False)
        if event.get_platform_name() != "aiocqhttp":
            await self._send(event, "首版仅支持 OneBot/aiocqhttp QQ 平台。")
            return
        if not self._initialized or self.commands is None:
            await self._send(event, "插件尚未完成初始化，请稍后重试。")
            return
        try:
            parsed = command or self.parser.parse(text, mentioned_users(event))
            if parsed is None:
                return
            actor_qq = str(event.get_sender_id() or "")
            if not actor_qq:
                raise CommandServiceError("无法识别 QQ 用户 ID")
            result = await self.commands.execute(
                actor_qq,
                parsed,
                origin_context=str(event.unified_msg_origin or ""),
            )
        except (
            AccountError,
            CatalogError,
            CommandParseError,
            CommandServiceError,
            LocalDataError,
            LoginSessionError,
            PlayerDataError,
            SyncError,
        ) as exc:
            result = str(exc)
        except Exception:
            logger.exception("%s：命令处理失败", PLUGIN_DISPLAY_NAME)
            result = "操作失败，请稍后重试；详细原因已记录到插件日志。"
        if isinstance(result, LoginLinkMessage):
            await self._send_login_link(event, result)
        elif isinstance(result, CardMessage):
            await self._send_card(event, result)
        else:
            await self._send(event, result)

    async def _html_render_file(
        self,
        template: str,
        data: dict[str, object],
        options: dict[str, object],
    ) -> str:
        return await self.html_render(
            template,
            data,
            return_url=False,
            options=options,
        )

    async def _fetch_static_image(self, url: str, max_bytes: int) -> bytes:
        return await self.http.get_bytes(
            url,
            allowed_hosts={"guide-res.aki-game.net"},
            max_bytes=max_bytes,
        )

    async def _fetch_catalog(self) -> object:
        return await self.http.get_json(
            "https://guide-server.aki-game.net/role/avatar/list",
            allowed_hosts={"guide-server.aki-game.net"},
            max_bytes=4 * 1024 * 1024,
        )

    async def _persist_config(self) -> None:
        saver = getattr(self.config, "save_config_async", None)
        if callable(saver):
            await saver()
            return
        self.config.save_config()

    async def _apply_settings(self, settings: PluginSettings) -> None:
        await self.public_login.update_settings(settings)
        await self.http.update_timeout(settings.request_timeout_seconds)
        self.settings = settings
        self.parser.update_settings(settings)
        self.web.settings = settings
        if self.repository is not None:
            self.repository.confirm_ttl_minutes = settings.confirm_ttl_minutes
        if self.accounts is not None:
            self.accounts.confirm_ttl_minutes = settings.confirm_ttl_minutes
        if self.login_sessions is not None:
            self.login_sessions.settings = settings
        if self.sync_service is not None:
            await self.sync_service.update_settings(settings)
        if self.card_renderer is not None:
            self.card_renderer.timeout_seconds = settings.render_timeout_seconds
        if self.commands is not None:
            self.commands.settings = settings

    def _apply_catalog(self, catalog: CharacterCatalog) -> None:
        self.catalog = catalog
        if self.sync_service is not None:
            self.sync_service.catalog = catalog
        if self.commands is not None:
            self.commands.catalog = catalog
            self.commands.cards.catalog = catalog

    @staticmethod
    async def _send_card(event: AstrMessageEvent, message: CardMessage) -> None:
        try:
            await event.send(MessageChain([Comp.Image.fromFileSystem(str(message.image_path))]))
        except Exception:
            logger.exception("%s：图片卡发送失败，降级为文本", PLUGIN_DISPLAY_NAME)
            await event.send(MessageChain([Comp.Plain(message.fallback_text)]))

    @staticmethod
    async def _send_login_link(event: AstrMessageEvent, message: LoginLinkMessage) -> None:
        text = (
            "请仅在可信设备上打开链接，勿转发或泄露。\n"
            f"链接将在 {message.expires_minutes} 分钟后过期，且只能使用一次。\n"
            f"{message.url}"
        )
        node = Comp.Node(
            uin=str(event.get_self_id() or "0"),
            name=PLUGIN_DISPLAY_NAME,
            content=[Comp.Plain(text)],
        )
        try:
            await event.send(MessageChain([Comp.Nodes([node])]))
        except Exception:
            logger.exception("%s：合并转发登录链接发送失败，降级为普通消息", PLUGIN_DISPLAY_NAME)
            await event.send(MessageChain([Comp.Plain(text)]))

    @staticmethod
    async def _send(event: AstrMessageEvent, text: str) -> None:
        await event.send(MessageChain([Comp.Plain(text)]))

    @staticmethod
    def _is_bot_message(event: AstrMessageEvent) -> bool:
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return True
        raw = getattr(event.message_obj, "raw_message", None)
        sender = raw.get("sender", {}) if isinstance(raw, dict) else {}
        return bool(sender.get("is_bot") or sender.get("is_robot"))

    async def terminate(self) -> None:
        self._initialized = False
        await self.public_login.close()
        await self.dashboard_web.close()
        if self.sync_service is not None:
            await self.sync_service.close()
        await self.http.close()
        await self.database.close()
        self.cipher = None
        self.repository = None
        self.accounts = None
        self.login_sessions = None
        self.sync_service = None
        self.player_data = None
        self.card_renderer = None
        self.commands = None
        self.dashboard_service = None
        self.backup_service = None
        logger.info("%s：已停止", PLUGIN_DISPLAY_NAME)


def plugin_runtime_snapshot(plugin: WuWaGlobalServerPlugin) -> dict[str, Any]:
    """为后续测试和诊断返回不含敏感值的运行状态。"""
    return {
        "initialized": plugin._initialized,
        "storage_root": str(plugin.paths.root),
        "http": plugin.http.status(),
    }
