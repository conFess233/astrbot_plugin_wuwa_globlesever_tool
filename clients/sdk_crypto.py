"""国际服 SDK 的纯编码与签名规则。"""

import base64
import hashlib

_CLIENT_SECRET = "32gh5r0p35ullmxrzzwk40ly"


class SdkEncodingError(ValueError):
    """表示无法编码 SDK 登录字段。"""


def encode_password(password: str) -> str:
    """复现国际服 SDK 客户端的 Base64 邻位交换编码。"""
    if not password:
        raise SdkEncodingError("密码不能为空")
    characters = list(base64.b64encode(password.encode()).decode())
    for offset in (0, 1):
        index = offset
        while index + 2 < len(characters):
            characters[index], characters[index + 2] = (
                characters[index + 2],
                characters[index],
            )
            if index + 6 >= len(characters):
                break
            index += 4
    return "".join(characters)


def generate_signature(fields: dict[str, str]) -> str:
    """按 SDK 规则排序参数，排除 sign/Geetest 后生成 MD5 签名。"""
    selected = (
        key
        for key in fields
        if key.casefold() != "sign" and not key.casefold().startswith("geetest")
    )
    body = "&".join(f"{key}={fields[key]}" for key in sorted(selected)) + _CLIENT_SECRET
    return hashlib.md5(body.encode(), usedforsecurity=False).hexdigest()
