"""与 AstrBot 解耦的确定性命令语法解析器。"""

import re
from dataclasses import dataclass
from enum import StrEnum

from ..services.settings import PluginSettings

_SPACE = re.compile(r"\s+")
_PAGE = re.compile(r"^(\d+)页$")
_MODIFY = re.compile(r"^修改\s+(.+?)\s+(武器等级|武器精炼|共鸣链|等级|武器)\s+(.+)$")
_RESET = re.compile(r"^重置\s+(.+?)\s+(武器等级|武器精炼|共鸣链|等级|武器|全部)$")


class CommandName(StrEnum):
    HELP = "help"
    LOGIN = "login"
    LOGIN_CONFIRM = "login_confirm"
    ACCOUNT = "account"
    SWITCH = "switch"
    SYNC = "sync"
    UNBIND = "unbind"
    CHARACTER_LIST = "character_list"
    CHARACTER_DETAIL = "character_detail"
    PROGRESS = "progress"
    MODIFY = "modify"
    RESET = "reset"
    CHARACTER_DELETE = "character_delete"
    LOCAL_MERGE = "local_merge"
    CLEAR_DATA = "clear_data"
    CONFIRM = "confirm"
    LANGUAGE = "language"


READ_ONLY_COMMANDS = {
    CommandName.CHARACTER_LIST,
    CommandName.CHARACTER_DETAIL,
    CommandName.PROGRESS,
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
        return None

    def _parse_formal(self, tail: str) -> ParsedCommand:
        if not tail or tail == "帮助":
            return ParsedCommand(CommandName.HELP)
        if tail == "登录":
            return ParsedCommand(CommandName.LOGIN)
        if tail.startswith("登录确认 "):
            return self._one_argument(CommandName.LOGIN_CONFIRM, tail, "登录确认")
        if tail == "账号":
            return ParsedCommand(CommandName.ACCOUNT)
        if tail.startswith("切换 "):
            return self._one_argument(CommandName.SWITCH, tail, "切换")
        if tail == "同步":
            return ParsedCommand(CommandName.SYNC)
        if tail.startswith("同步 "):
            return self._one_argument(CommandName.SYNC, tail, "同步")
        if tail.startswith("解绑 "):
            return self._one_argument(CommandName.UNBIND, tail, "解绑")
        if tail == "角色":
            return ParsedCommand(CommandName.CHARACTER_LIST, ("1",))
        if tail.startswith("角色 "):
            return self._character_tail(tail.removeprefix("角色 ").strip())
        if tail == "练度":
            return ParsedCommand(CommandName.PROGRESS)

        match = _MODIFY.fullmatch(tail)
        if match:
            return ParsedCommand(CommandName.MODIFY, tuple(part.strip() for part in match.groups()))
        match = _RESET.fullmatch(tail)
        if match:
            return ParsedCommand(CommandName.RESET, tuple(part.strip() for part in match.groups()))
        if tail.startswith("角色删除 "):
            return self._one_argument(CommandName.CHARACTER_DELETE, tail, "角色删除")
        if tail.startswith("本地合并 "):
            return self._one_argument(CommandName.LOCAL_MERGE, tail, "本地合并")
        if tail == "清除数据":
            return ParsedCommand(CommandName.CLEAR_DATA)
        if tail.startswith("确认 "):
            return self._one_argument(CommandName.CONFIRM, tail, "确认")
        if tail.startswith("语言 "):
            return self._one_argument(CommandName.LANGUAGE, tail, "语言")
        raise CommandParseError("命令格式不正确，请使用 /kh 帮助 查看用法")

    def _parse_keyword(self, text: str) -> ParsedCommand | None:
        exact_groups = (
            (self.settings.keyword_help, CommandName.HELP),
            (self.settings.keyword_login, CommandName.LOGIN),
            (self.settings.keyword_account, CommandName.ACCOUNT),
            (self.settings.keyword_progress, CommandName.PROGRESS),
        )
        for keywords, name in exact_groups:
            if any(text.casefold() == keyword.casefold() for keyword in keywords):
                return ParsedCommand(name, trigger="keyword")

        for keyword in sorted(self.settings.keyword_character, key=len, reverse=True):
            if text.casefold() == keyword.casefold():
                return ParsedCommand(CommandName.CHARACTER_LIST, ("1",), trigger="keyword")
            if text[: len(keyword)].casefold() != keyword.casefold():
                continue
            remainder = text[len(keyword) :]
            if remainder.startswith(" "):
                result = self._character_tail(remainder.strip())
                return ParsedCommand(result.name, result.arguments, trigger="keyword")
            if _PAGE.fullmatch(remainder):
                page = self._page_number(remainder)
                return ParsedCommand(CommandName.CHARACTER_LIST, (page,), trigger="keyword")
        return None

    def _character_tail(self, value: str) -> ParsedCommand:
        if _PAGE.fullmatch(value):
            return ParsedCommand(CommandName.CHARACTER_LIST, (self._page_number(value),))
        if not value:
            raise CommandParseError("角色参数不能为空")
        return ParsedCommand(CommandName.CHARACTER_DETAIL, (value,))

    @staticmethod
    def _page_number(value: str) -> str:
        match = _PAGE.fullmatch(value)
        if not match or int(match.group(1)) <= 0:
            raise CommandParseError("页码必须是正整数")
        return match.group(1)

    @staticmethod
    def _one_argument(name: CommandName, tail: str, prefix: str) -> ParsedCommand:
        value = tail.removeprefix(prefix).strip()
        if not value:
            raise CommandParseError(f"{prefix} 参数不能为空")
        return ParsedCommand(name, (value,))
