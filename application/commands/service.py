"""命令应用服务：校验权限、协调刷新并生成展示结果。"""

from dataclasses import replace

from ...domain.cards import CardMessage
from ...domain.catalog import CharacterCatalog
from ...domain.login import LoginLinkMessage
from ...domain.player import PlayerDataError
from ...infrastructure.database.repositories import AccountRepository, LocalDataRepository
from ...infrastructure.database.repositories.local_data import CharacterRecord
from ...presentation.commands import CommandName, ParsedCommand
from ..cards import CardService
from ..login.service import LoginSessionService
from ..refresh.player import PlayerDataService
from ..refresh.roles import GuideSyncService
from ..settings import PluginSettings


class CommandServiceError(ValueError):
    """表示命令语义或权限不满足。"""


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
        is_admin: bool = False,
    ) -> str | LoginLinkMessage | CardMessage:
        if command.target_qq == actor_qq:
            command = replace(command, target_qq=None)
        target_qq = command.target_qq or actor_qq
        if command.target_qq and not self.settings.allow_query_others:
            raise CommandServiceError("管理员未开启查询他人数据功能")

        if self.accounts is not None:
            await self.accounts.touch_origin(actor_qq, origin_context)

        if command.name == CommandName.HELP:
            return await self.cards.help()
        if command.name == CommandName.LOGIN:
            if self.login_sessions is None:
                raise CommandServiceError("网页登录服务尚未初始化")
            return await self.login_sessions.create_link(actor_qq, origin_context)
        if command.name == CommandName.CANCEL_LOGIN:
            if self.login_sessions is None:
                raise CommandServiceError("网页登录服务尚未初始化")
            cancelled = await self.login_sessions.cancel(actor_qq)
            return "登录链接已作废" if cancelled else "当前没有可取消的登录链接"
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
            pending = await self.accounts.begin_unbind(
                actor_qq, command.arguments[0], origin_context
            )
            return self._confirmation_text(f"解绑 {pending.region_id} 区服 UID {pending.uid}")
        if command.name == CommandName.REFRESH:
            if self.sync_service is None:
                raise CommandServiceError("攻略站同步服务尚未初始化")
            uid = command.arguments[0] if command.arguments else None
            result = await self.sync_service.sync(actor_qq, uid, force=is_admin)
            account_note = ""
            if self.player_data is not None:
                try:
                    snapshot = await self.player_data.refresh(
                        actor_qq,
                        uid=result.uid,
                        region_id=result.region_id,
                    )
                    if snapshot.is_cached_fallback:
                        account_note = "\n账号详情刷新失败，已保留本地缓存。"
                except PlayerDataError as exc:
                    account_note = f"\n账号详情刷新失败：{exc}"
            return (
                f"{result.region_id} 区服 UID {result.uid} 刷新成功，"
                f"共获取 {result.owned_count} 个角色。{account_note}"
            )
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
            return await self._reset(actor_qq, command, origin_context)
        if command.name == CommandName.CHARACTER_DELETE:
            return await self._begin_delete(actor_qq, command, origin_context)
        if command.name == CommandName.CONFIRM:
            if self.accounts is not None:
                result = await self.accounts.confirm_unbind(actor_qq, origin_context)
                if result is not None:
                    return result
            return await self.repository.confirm(actor_qq, origin_context)
        if command.name == CommandName.CANCEL:
            return await self.repository.cancel(actor_qq, origin_context)
        raise CommandServiceError("该命令尚未接入")

    async def _account(self, qq_id: str) -> str:
        overview = await self.accounts.overview(qq_id)
        active = (
            "本地"
            if overview.active_is_local
            else next(
                (
                    f"{account.region_name} · UID {account.uid}"
                    for account in overview.accounts
                    if account.is_active
                ),
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
        for index, account in enumerate(overview.accounts, start=1):
            marks = []
            if account.is_default:
                marks.append("默认")
            if account.is_active:
                marks.append("当前")
            suffix = f" [{' / '.join(marks)}]" if marks else ""
            name = f" {account.player_name}" if account.player_name else ""
            lines.append(
                f"{index}. {account.region_name} · UID {account.uid}{name} · "
                f"{account.sync_status}{suffix}"
            )
        return "\n".join(lines)

    async def _character_list(self, qq_id: str, command: ParsedCommand) -> str | CardMessage:
        profile = await self.repository.active_profile(
            qq_id, external_query=command.target_qq is not None
        )
        if (
            command.target_qq
            and profile.profile_type == "uid"
            and not await self.repository.role_snapshot_updated_at(profile.profile_id)
        ):
            raise CommandServiceError("该用户尚无角色数据缓存，请让对方先执行 /kh 刷新")
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
            qq_id,
        )

    async def _character_detail(self, qq_id: str, command: ParsedCommand) -> str | CardMessage:
        character = self.catalog.resolve(command.arguments[0])
        profile = await self.repository.active_profile(
            qq_id, external_query=command.target_qq is not None
        )
        if (
            command.target_qq
            and profile.profile_type == "uid"
            and not await self.repository.role_snapshot_updated_at(profile.profile_id)
        ):
            raise CommandServiceError("该用户尚无角色数据缓存，请让对方先执行 /kh 刷新")
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
            result = await self.cards.account_info(snapshot, qq_id)
        elif kind == "daily":
            result = await self.cards.daily(snapshot, qq_id)
        else:
            result = await self.cards.exploration(snapshot, qq_id)
        if snapshot.is_cached_fallback:
            if isinstance(result, CardMessage):
                return replace(result, notice="刷新失败，已展示缓存")
            return f"{result}\n刷新失败，已展示缓存"
        return result

    async def _modify(self, qq_id: str, command: ParsedCommand) -> str:
        character_query, field, raw_value = command.arguments
        character = self.catalog.resolve_exact(character_query)
        profile = await self.repository.active_profile(qq_id)
        existing = await self.repository.get_character(profile.profile_id, character.character_id)
        if character.is_rover and (
            existing is None or existing.record_origin not in {"api", "mixed"}
        ):
            raise CommandServiceError("只能修改接口实际返回的漂泊者形态")
        if field == "武器" and existing is not None and existing.weapon_source == "api":
            raise CommandServiceError("接口已返回该角色的武器，不能用本地武器覆盖")
        if field in {"武器等级", "武器精炼"} and (existing is None or existing.weapon_id is None):
            raise CommandServiceError("请先为该角色设置武器")
        if field in {"等级", "武器等级"}:
            value: int | str = self._bounded_integer(raw_value, 1, 90)
        elif field == "共鸣链":
            value = self._bounded_integer(raw_value, 0, 6)
        elif field == "武器精炼":
            value = self._bounded_integer(raw_value, 1, 5)
        else:
            value = raw_value.strip()
            if not value or len(value) > 80:
                raise CommandServiceError("武器名称必须为 1-80 个字符")
        record = await self.repository.set_manual_field(qq_id, character, field, value)
        return (
            f"已将 {record.character_name} 的{field}修改为 {value}。\n"
            f"{self._detail_text('活动', record)}"
        )

    async def _reset(self, qq_id: str, command: ParsedCommand, origin_context: str) -> str:
        character = self.catalog.resolve_exact(command.arguments[0])
        field = command.arguments[1]
        if field == "全部":
            pending = await self.repository.begin_reset_all(
                qq_id,
                character.character_id,
                character.display_name,
                origin_context,
            )
            return self._confirmation_text(pending.summary)
        record = await self.repository.reset_manual_fields(qq_id, character.character_id, field)
        if record is None:
            return f"已重置 {character.display_name} 的{field}；该纯手动空记录已移除。"
        return f"已重置 {character.display_name} 的{field}。\n{self._detail_text('活动', record)}"

    async def _begin_delete(self, qq_id: str, command: ParsedCommand, origin_context: str) -> str:
        character = self.catalog.resolve_exact(command.arguments[0])
        pending = await self.repository.begin_character_delete(
            qq_id, character.character_id, origin_context
        )
        return self._confirmation_text(pending.summary)

    @staticmethod
    def _confirmation_text(operation: str) -> str:
        return f"危险操作：{operation}\n请在 60 秒内于当前会话发送 /kh 确认；发送 /kh 取消可撤销。"

    @staticmethod
    def _detail_text(profile_label: str, record: CharacterRecord) -> str:
        return (
            f"{record.character_name}（{profile_label}档案）\n"
            f"等级：{CommandService._value(record.level)}\n"
            f"共鸣链：{CommandService._value(record.chain)}\n"
            f"武器：{record.weapon_name or '---'}\n"
            f"武器等级：{CommandService._value(record.weapon_level)}\n"
            f"武器精炼：{CommandService._value(record.weapon_refinement)}"
        )

    @staticmethod
    def _value(value: object | None) -> str:
        return "---" if value is None else str(value)

    @staticmethod
    def _bounded_integer(raw: str, minimum: int, maximum: int) -> int:
        try:
            value = int(raw)
        except ValueError as exc:
            raise CommandServiceError(f"数值必须是 {minimum}-{maximum} 的整数") from exc
        if not minimum <= value <= maximum:
            raise CommandServiceError(f"数值必须是 {minimum}-{maximum} 的整数")
        return value
