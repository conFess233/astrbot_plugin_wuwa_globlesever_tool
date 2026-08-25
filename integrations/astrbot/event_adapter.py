"""从 AstrBot 消息段提取纯文本和非 Bot 查询对象。"""

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent


def plain_text(event: AstrMessageEvent) -> str:
    return " ".join(
        str(item.text).strip()
        for item in event.get_messages()
        if isinstance(item, Comp.Plain) and str(item.text).strip()
    ).strip()


def mentioned_users(event: AstrMessageEvent) -> list[str]:
    self_id = str(event.get_self_id() or "")
    result: list[str] = []
    for item in event.get_messages():
        if not isinstance(item, Comp.At):
            continue
        target = str(item.qq)
        if target not in {"", "all", self_id} and target not in result:
            result.append(target)
    return result
