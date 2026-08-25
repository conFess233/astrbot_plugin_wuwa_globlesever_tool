"""与 AstrBot 解耦的确定性命令语法解析器。"""

import re
from dataclasses import dataclass
from enum import StrEnum

from ...application.settings import PluginSettings

_SPACE = re.compile(r"\s+")
_PAGE = re.compile(r"^(?:\d+|x)页?$", re.IGNORECASE)
_MODIFY = re.compile(r"^修改\s+(.+?)\s+(武器等级|武器精炼|共鸣链|等级|武器)\s+(.+)$")
_RESET = re.compile(r"^重置\s+(.+?)\s+(武器等级|武器精炼|共鸣链|等级|武器|全部)$")


class CommandName(StrEnum):
    HELP = "help"
    LOGIN = "login"
    CANCEL_LOGIN = "cancel_login"
    ACCOUNT = "account"
    SWITCH = "switch"
    REFRESH = "refresh"
    UNBIND = "unbind"
    CHARACTER_LIST = "character_list"
    CHARACTER_DETAIL = "character_detail"
    ACCOUNT_INFO = "account_info"
    DAILY = "daily"
    EXPLORATION = "exploration"
    MODIFY = "modify"
    RESET = "reset"
    CHARACTER_DELETE = "character_delete"
    CONFIRM = "confirm"
    CANCEL = "cancel"


READ_ONLY_COMMANDS = {
    CommandName.CHARACTER_LIST,
    CommandName.CHARACTER_DETAIL,
    CommandName.ACCOUNT_INFO,
    CommandName.EXPLORATION,
}


class CommandParseError(ValueError):
    """表示消息已命中插件入口，但语法或目标不合法。"""


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: CommandName
    arguments: tuple[str, ...] = ()
    target_qq: str | None = None
    trigger: str = "command"


class CommandParser:
    def __init__(self, settings: PluginSettings):
        self.update_settings(settings)

    def update_settings(self, settings: PluginSettings) -> None:
        self.settings = settings
        self.roots = ("/kh", *settings.extra_command_roots)
        groups = (
            (settings.keyword_help, CommandName.HELP, False),
            (settings.keyword_cancel_login, CommandName.CANCEL_LOGIN, False),
            (settings.keyword_login, CommandName.LOGIN, False),
            (settings.keyword_account_info, CommandName.ACCOUNT_INFO, False),
            (settings.keyword_account, CommandName.ACCOUNT, False),
            (settings.keyword_switch, CommandName.SWITCH, True),
            (settings.keyword_character, CommandName.CHARACTER_LIST, True),
            (settings.keyword_daily, CommandName.DAILY, False),
            (settings.keyword_exploration, CommandName.EXPLORATION, False),
            (settings.keyword_refresh, CommandName.REFRESH, True),
        )
        self.keyword_registry = tuple(
            sorted(
                (
                    (keyword, name, accepts_argument)
                    for keywords, name, accepts_argument in groups
                    for keyword in keywords
                ),
                key=lambda item: len(item[0]),
                reverse=True,
            )
        )

    def parse(self, plain_text: str, mentioned_users: list[str]) -> ParsedCommand | None:
        text = _SPACE.sub(" ", plain_text.strip())
        if not text:
            return None
        if len(mentioned_users) > 1:
            raise CommandParseError("一次只能指定一个查询对象")

        formal_tail = self._formal_tail(text)
        if formal_tail is not None:
            command = self._parse_formal(formal_tail)
        else:
            command = self._parse_keyword(text)
        if command is None:
            return None

        target = mentioned_users[0] if mentioned_users else None
        if target and command.name not in READ_ONLY_COMMANDS:
            raise CommandParseError("该操作不能指定其他用户")
        return ParsedCommand(command.name, command.arguments, target, command.trigger)

    def _formal_tail(self, text: str) -> str | None:
        for root in sorted(self.roots, key=len, reverse=True):
            if text.casefold() == root.casefold():
                return ""
            prefix = f"{root} "
            if text[: len(prefix)].casefold() == prefix.casefold():
                return text[len(prefix) :].strip()
            if text[: len(root)].casefold() == root.casefold():
                compact_tail = text[len(root) :].strip()
                if compact_tail in {
                    "账号",
                    "账号信息",
                    "角色",
                    "日常",
                    "探索",
                    "登录",
                    "取消登录",
                    "刷新",
                    "同步",
                    "帮助",
                }:
                    return compact_tail
        return None

    def _parse_formal(self, tail: str) -> ParsedCommand:
        if not tail or tail == "帮助":
            return ParsedCommand(CommandName.HELP)
        if tail == "登录":
            return ParsedCommand(CommandName.LOGIN)
        if tail == "取消登录":
            return ParsedCommand(CommandName.CANCEL_LOGIN)
        if tail == "账号":
            return ParsedCommand(CommandName.ACCOUNT)
        if tail == "账号信息":
            return ParsedCommand(CommandName.ACCOUNT_INFO)
        if tail.startswith("切换 "):
            return self._one_argument(CommandName.SWITCH, tail, "切换")
        if tail in {"刷新", "同步"}:
            return ParsedCommand(CommandName.REFRESH)
        if tail.startswith("刷新 "):
            return self._one_argument(CommandName.REFRESH, tail, "刷新")
        if tail.startswith("同步 "):
            return self._one_argument(CommandName.REFRESH, tail, "同步")
        if tail.startswith("解绑 "):
            return self._one_argument(CommandName.UNBIND, tail, "解绑")
        if tail == "角色":
            return ParsedCommand(CommandName.CHARACTER_LIST)
        if tail.startswith("角色 "):
            return self._character_tail(tail.removeprefix("角色 ").strip())
        if tail == "日常":
            return ParsedCommand(CommandName.DAILY)
        if tail == "探索":
            return ParsedCommand(CommandName.EXPLORATION)
        if tail in {"练度", "面板"}:
            raise CommandParseError("该命令已移除，请使用 /kh 角色")

        match = _MODIFY.fullmatch(tail)
        if match:
            return ParsedCommand(CommandName.MODIFY, tuple(part.strip() for part in match.groups()))
        match = _RESET.fullmatch(tail)
        if match:
            return ParsedCommand(CommandName.RESET, tuple(part.strip() for part in match.groups()))
        if tail.startswith("删除角色 "):
            return self._one_argument(CommandName.CHARACTER_DELETE, tail, "删除角色")
        if tail == "确认":
            return ParsedCommand(CommandName.CONFIRM)
        if tail == "取消":
            return ParsedCommand(CommandName.CANCEL)
        raise CommandParseError("命令格式不正确，请使用 /kh 帮助 查看用法")

    def _parse_keyword(self, text: str) -> ParsedCommand | None:
        if text.casefold() in {"kh练度", "鸣潮练度", "kh面板", "鸣潮面板"}:
            raise CommandParseError("该命令已移除，请使用 /kh 角色")
        for keyword, name, accepts_argument in self.keyword_registry:
            folded = keyword.casefold()
            if text.casefold() == folded:
                if name == CommandName.SWITCH:
                    raise CommandParseError("切换参数不能为空")
                return ParsedCommand(name, trigger="keyword")
            if text[: len(keyword)].casefold() != folded:
                continue
            remainder = text[len(keyword) :]
            if name == CommandName.CHARACTER_LIST and _PAGE.fullmatch(remainder):
                raise CommandParseError("角色列表已取消分页，请直接使用 /kh 角色")
            if not accepts_argument or not remainder.startswith(" "):
                continue
            argument = remainder.strip()
            if not argument:
                raise CommandParseError(f"{keyword} 参数不能为空")
            if name == CommandName.CHARACTER_LIST:
                result = self._character_tail(argument)
                return ParsedCommand(result.name, result.arguments, trigger="keyword")
            return ParsedCommand(name, (argument,), trigger="keyword")
        return None

    def _character_tail(self, value: str) -> ParsedCommand:
        if _PAGE.fullmatch(value):
            raise CommandParseError("角色列表已取消分页，请直接使用 /kh 角色")
        if not value:
            raise CommandParseError("角色参数不能为空")
        return ParsedCommand(CommandName.CHARACTER_DETAIL, (value,))

    @staticmethod
    def _one_argument(name: CommandName, tail: str, prefix: str) -> ParsedCommand:
        value = tail.removeprefix(prefix).strip()
        if not value:
            raise CommandParseError(f"{prefix} 参数不能为空")
        return ParsedCommand(name, (value,))
