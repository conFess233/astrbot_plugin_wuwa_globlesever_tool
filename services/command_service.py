"""命令应用服务：校验权限、操作本地档案并生成文本结果。"""

from ..commands.parser import CommandName, ParsedCommand
from ..domain.cards import CardMessage
from ..domain.login import LoginLinkMessage
from ..domain.player import PlayerDataError
from ..repositories.accounts import AccountRepository
from ..repositories.local_data import CharacterRecord, LocalDataRepository
from .cards import CardService
from .catalog import CharacterCatalog
from .login_sessions import LoginSessionService
from .player_data import PlayerDataService
from .settings import PluginSettings
from .sync import GuideSyncService, SyncError


class CommandServiceError(ValueError):
    """表示命令语义或权限不满足。"""


_LANGUAGES = {"zh-CN", "zh-TW", "en", "ja", "ko"}
_NOT_IMPLEMENTED = {
    CommandName.LOCAL_MERGE: "本地档案合并当前版本尚未实现。",
}


class CommandService:
    def __init__(
        self,
        repository: LocalDataRepository,
        catalog: CharacterCatalog,
        settings: PluginSettings,
        login_sessions: LoginSessionService | None = None,
        accounts: AccountRepository | None = None,
        sync_service: GuideSyncService | None = None,
        player_data: PlayerDataService | None = None,
        cards: CardService | None = None,
    ):
        self.repository = repository
        self.catalog = catalog
        self.settings = settings
        self.login_sessions = login_sessions
        self.accounts = accounts
        self.sync_service = sync_service
        self.player_data = player_data
        self.cards = cards or CardService(catalog)

    async def execute(
        self,
        actor_qq: str,
        command: ParsedCommand,
        *,
        origin_context: str = "",
    ) -> str | LoginLinkMessage | CardMessage:
        target_qq = command.target_qq or actor_qq
        if command.target_qq and not self.settings.allow_query_others:
            raise CommandServiceError("管理员未开启查询他人数据功能")

        if command.name == CommandName.HELP:
            return self._help()
        if command.name in _NOT_IMPLEMENTED:
            return _NOT_IMPLEMENTED[command.name]
        if command.name == CommandName.LOGIN:
            if self.login_sessions is None:
                raise CommandServiceError("网页登录服务尚未初始化")
            return await self.login_sessions.create_link(actor_qq, origin_context)
        if command.name == CommandName.LOGIN_CONFIRM:
            if self.login_sessions is None:
                raise CommandServiceError("网页登录服务尚未初始化")
            result = await self.login_sessions.confirm_login(
                actor_qq,
                origin_context,
                command.arguments[0],
            )
            uids = "、".join(
                f"{uid}（默认）" if uid == result.default_uid else uid
                for uid in result.selected_uids
            )
            message = f"国际服账号绑定成功\n账号：{result.email_masked}\nUID：{uids}"
            if self.sync_service is None:
                return f"{message}\n角色数据：同步服务尚未初始化"
            try:
                synced = await self.sync_service.sync(actor_qq, result.default_uid)
                if self.player_data is not None:
                    try:
                        await self.player_data.query(actor_qq, uid=result.default_uid)
                    except PlayerDataError as exc:
                        return (
                            f"{message}\n首次同步：{synced.owned_count} 个角色"
                            f"\n账号详情刷新失败：{exc}"
                        )
                return f"{message}\n首次同步：{synced.owned_count} 个角色"
            except SyncError as exc:
                return f"{message}\n首次同步失败：{exc}"
        if command.name == CommandName.ACCOUNT:
            if self.accounts is None:
                profile = await self.repository.active_profile(actor_qq)
                return f"当前活动档案：{profile.label}\n尚未绑定国际服 UID。"
            return await self._account(actor_qq)
        if command.name == CommandName.SWITCH:
            if self.accounts is None:
                if command.arguments[0].casefold() != "本地":
                    return "账号服务尚未初始化；当前仅支持 /kh 切换 本地。"
                profile = await self.repository.switch_local(actor_qq)
                return f"已切换到{profile.label}档案。"
            label = await self.accounts.switch(actor_qq, command.arguments[0])
            return f"已切换到{label}档案。"
        if command.name == CommandName.UNBIND:
            if self.accounts is None:
                raise CommandServiceError("账号服务尚未初始化")
            pending = await self.accounts.begin_unbind(actor_qq, command.arguments[0])
            return self._confirmation_text(f"解绑 UID {pending.uid}", pending.code)
        if command.name == CommandName.SYNC:
            if self.sync_service is None:
                raise CommandServiceError("攻略站同步服务尚未初始化")
            uid = command.arguments[0] if command.arguments else None
            result = await self.sync_service.sync(actor_qq, uid)
            account_note = ""
            if self.player_data is not None:
                try:
                    await self.player_data.query(actor_qq, uid=result.uid)
                except PlayerDataError as exc:
                    account_note = f"\n账号详情刷新失败：{exc}"
            return f"UID {result.uid} 同步成功，共获取 {result.owned_count} 个角色。{account_note}"
        if command.name == CommandName.CHARACTER_LIST:
            return await self._character_list(target_qq, command)
        if command.name == CommandName.CHARACTER_DETAIL:
            return await self._character_detail(target_qq, command)
        if command.name == CommandName.ACCOUNT_INFO:
            return await self._player_card(target_qq, command, "account_info")
        if command.name == CommandName.DAILY:
            return await self._player_card(actor_qq, command, "daily")
        if command.name == CommandName.EXPLORATION:
            return await self._player_card(target_qq, command, "exploration")
        if command.name == CommandName.MODIFY:
            return await self._modify(actor_qq, command)
        if command.name == CommandName.RESET:
            return await self._reset(actor_qq, command)
        if command.name == CommandName.CHARACTER_DELETE:
            return await self._begin_delete(actor_qq, command)
        if command.name == CommandName.CLEAR_DATA:
            pending = await self.repository.begin_clear_data(actor_qq)
            return self._confirmation_text("清除你的全部插件数据", pending.code)
        if command.name == CommandName.CONFIRM:
            if self.accounts is not None:
                result = await self.accounts.confirm_unbind(actor_qq, command.arguments[0])
                if result is not None:
                    return result
            return await self.repository.confirm(actor_qq, command.arguments[0])
        if command.name == CommandName.LANGUAGE:
            language = command.arguments[0]
            if language not in _LANGUAGES:
                raise CommandServiceError("语言仅支持 zh-CN、zh-TW、en、ja、ko")
            await self.repository.set_language(actor_qq, language)
            return f"语言已设置为 {language}。首版命令文本暂以简体中文显示。"
        raise CommandServiceError("该命令尚未接入")

    async def _account(self, qq_id: str) -> str:
        overview = await self.accounts.overview(qq_id)
        active = (
            "本地"
            if overview.active_is_local
            else next(
                (account.uid for account in overview.accounts if account.is_active),
                "---",
            )
        )
        lines = [f"当前活动档案：{active}"]
        if overview.email_masked:
            lines.append(f"登录账号：{'、'.join(overview.email_masked)}")
        if not overview.accounts:
            lines.append("尚未绑定国际服 UID。")
            return "\n".join(lines)
        lines.append("已绑定 UID：")
        for account in overview.accounts:
            marks = []
            if account.is_default:
                marks.append("默认")
            if account.is_active:
                marks.append("当前")
            suffix = f" [{' / '.join(marks)}]" if marks else ""
            name = f" {account.player_name}" if account.player_name else ""
            lines.append(
                f"- {account.uid}{name} · {account.region_name} · {account.sync_status}{suffix}"
            )
        return "\n".join(lines)

    async def _character_list(self, qq_id: str, command: ParsedCommand) -> str | CardMessage:
        profile = await self.repository.active_profile(
            qq_id, external_query=command.target_qq is not None
        )
        records = await self.repository.list_characters(profile.profile_id)
        player = None
        if profile.uid and self.player_data is not None:
            try:
                player = await self.player_data.cached(
                    qq_id, external_query=command.target_qq is not None
                )
            except PlayerDataError:
                player = None
        heading = "角色总览"
        return await self.cards.character_list(
            profile,
            heading,
            len(records),
            records,
            player,
        )

    async def _character_detail(self, qq_id: str, command: ParsedCommand) -> str | CardMessage:
        character = self.catalog.resolve(command.arguments[0])
        profile = await self.repository.active_profile(
            qq_id, external_query=command.target_qq is not None
        )
        record = await self.repository.get_character(profile.profile_id, character.character_id)
        if record is None:
            raise CommandServiceError(f"{profile.label}档案中没有 {character.display_name} 的记录")
        profile_label = "目标用户" if command.target_qq else profile.label
        return await self.cards.character_detail(profile, profile_label, record)

    async def _player_card(
        self,
        qq_id: str,
        command: ParsedCommand,
        kind: str,
    ) -> str | CardMessage:
        if self.player_data is None:
            raise CommandServiceError("玩家详情服务尚未初始化")
        snapshot = await self.player_data.query(qq_id, external_query=command.target_qq is not None)
        if kind == "account_info":
            return await self.cards.account_info(snapshot)
        if kind == "daily":
            return await self.cards.daily(snapshot)
        return await self.cards.exploration(snapshot)

    async def _modify(self, qq_id: str, command: ParsedCommand) -> str:
        character_query, field, raw_value = command.arguments
        character = self.catalog.resolve(character_query)
        if field in {"武器", "武器等级", "武器精炼"}:
            raise CommandServiceError("武器静态目录尚未完成验证，本阶段暂不接受武器修改")
        value = (
            self._bounded_integer(raw_value, 1, 90)
            if field == "等级"
            else self._bounded_integer(raw_value, 0, 6)
        )
        record = await self.repository.set_manual_field(qq_id, character, field, value)
        return (
            f"已将 {record.character_name} 的{field}修改为 {value}。\n"
            f"{self._detail_text('活动', record)}"
        )

    async def _reset(self, qq_id: str, command: ParsedCommand) -> str:
        character = self.catalog.resolve(command.arguments[0])
        field = command.arguments[1]
        record = await self.repository.reset_manual_fields(qq_id, character.character_id, field)
        if record is None:
            return f"已重置 {character.display_name} 的{field}；该纯手动空记录已移除。"
        return f"已重置 {character.display_name} 的{field}。\n{self._detail_text('活动', record)}"

    async def _begin_delete(self, qq_id: str, command: ParsedCommand) -> str:
        character = self.catalog.resolve(command.arguments[0])
        pending = await self.repository.begin_character_delete(qq_id, character.character_id)
        return self._confirmation_text(f"删除 {character.display_name} 的当前记录", pending.code)

    def _confirmation_text(self, operation: str, code: str) -> str:
        return (
            f"危险操作：{operation}\n"
            f"确认码：{code}\n"
            f"请在 {self.settings.confirm_ttl_minutes} 分钟内发送 /kh 确认 {code}。"
        )

    @staticmethod
    def _detail_text(profile_label: str, record: CharacterRecord) -> str:
        return (
            f"{record.character_name}（{profile_label}档案）\n"
            f"等级：{CommandService._value(record.level)}\n"
            f"共鸣链：{CommandService._value(record.chain)}\n"
            f"武器：{record.weapon_id or '---'}\n"
            f"武器等级：{CommandService._value(record.weapon_level)}\n"
            f"武器精炼：{CommandService._value(record.weapon_refinement)}\n"
            f"评分：{CommandService._score(record)}"
        )

    @staticmethod
    def _value(value: object | None) -> str:
        return "---" if value is None else str(value)

    @staticmethod
    def _score(record: CharacterRecord) -> str:
        if record.score_total is None:
            return "---"
        grade = f" {record.score_grade}" if record.score_grade else ""
        return f"{record.score_total:g}{grade}"

    @staticmethod
    def _bounded_integer(raw: str, minimum: int, maximum: int) -> int:
        try:
            value = int(raw)
        except ValueError as exc:
            raise CommandServiceError(f"数值必须是 {minimum}-{maximum} 的整数") from exc
        if not minimum <= value <= maximum:
            raise CommandServiceError(f"数值必须是 {minimum}-{maximum} 的整数")
        return value

    @staticmethod
    def _help() -> str:
        return (
            "鸣潮国际服数据工具\n"
            "/kh 角色 [@用户]\n"
            "/kh 角色 <角色名|角色ID> [@用户]\n"
            "/kh 账号信息 [@用户]\n"
            "/kh 日常\n"
            "/kh 探索 [@用户]\n"
            "/kh 修改 <角色> 等级 <1-90>\n"
            "/kh 修改 <角色> 共鸣链 <0-6>\n"
            "/kh 重置 <角色> <等级|共鸣链|武器|武器等级|武器精炼|全部>\n"
            "/kh 角色删除 <角色>\n"
            "/kh 登录 | /kh 登录确认 <6位确认码>\n"
            "/kh 账号 | /kh 切换 <UID|本地> | /kh 解绑 <UID>\n"
            "/kh 同步 [UID]\n"
            "/kh 清除数据 | /kh 确认 <确认码>\n"
            "/kh 语言 <zh-CN|zh-TW|en|ja|ko>\n"
            "兼容关键词：kh角色、kh角色 <角色>、kh账号信息、kh日常、kh探索。"
        )
