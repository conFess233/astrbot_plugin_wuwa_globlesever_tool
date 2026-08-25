"""攻略站请求使用的稳定浏览器标识与响应类型判断。"""

from __future__ import annotations

from collections.abc import Mapping

GUIDE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)


def guide_headers(language: str, token: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": GUIDE_USER_AGENT,
        "x-language": language,
        "Accept-Language": language,
    }
    if token:
        headers["x-token"] = token
    return headers


def is_json_response(headers: Mapping[str, str]) -> bool:
    content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
    return content_type == "application/json" or content_type.endswith("+json")
