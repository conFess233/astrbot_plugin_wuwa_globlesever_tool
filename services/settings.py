"""把 AstrBot 原始配置转换为经过边界校验的运行设置。"""

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

_ROOT_PATTERN = re.compile(r"^/[A-Za-z0-9_-]{1,20}$")
SUPPORTED_LANGUAGES = ("zh-CN", "zh-TW", "en", "ja", "ko")


class SettingsError(ValueError):
    """表示插件配置无效。"""


@dataclass(frozen=True, slots=True)
class PluginSettings:
    public_https_base_url: str
    login_server_host: str
    login_server_port: int
    login_trust_proxy_headers: bool
    login_trusted_proxy_cidrs: tuple[str, ...]
    extra_command_roots: tuple[str, ...]
    keyword_help: tuple[str, ...]
    keyword_login: tuple[str, ...]
    keyword_account: tuple[str, ...]
    keyword_account_info: tuple[str, ...]
    keyword_character: tuple[str, ...]
    keyword_daily: tuple[str, ...]
    keyword_exploration: tuple[str, ...]
    allow_query_others: bool
    login_link_ttl_minutes: int
    confirm_ttl_minutes: int
    confirm_max_attempts: int
    login_rate_window_minutes: int
    login_session_max_attempts: int
    login_email_max_attempts: int
    login_ip_max_attempts: int
    login_freeze_minutes: int
    auto_sync_enabled: bool
    auto_sync_interval_hours: int
    sync_concurrency: int
    role_detail_concurrency: int
    request_timeout_seconds: int
    render_timeout_seconds: int
    admin_audit_retention_days: int

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "PluginSettings":
        public_url = str(values.get("public_https_base_url") or "").strip().rstrip("/")
        if public_url:
            parsed = urlparse(public_url)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise SettingsError("登录页公网地址必须是不含路径或查询参数的 HTTPS 根地址")

        login_server_host = str(values.get("login_server_host") or "127.0.0.1").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", login_server_host):
            raise SettingsError("登录监听地址格式无效")

        roots = tuple(
            root
            for root in cls._normalized_roots(values.get("extra_command_roots", []))
            if root.casefold() != "/kh"
        )
        return cls(
            public_https_base_url=public_url,
            login_server_host=login_server_host,
            login_server_port=cls._bounded_int(values, "login_server_port", 6199, 1024, 65535),
            login_trust_proxy_headers=bool(values.get("login_trust_proxy_headers", True)),
            login_trusted_proxy_cidrs=cls._networks(values.get("login_trusted_proxy_cidrs", [])),
            extra_command_roots=roots,
            keyword_help=cls._keywords(values, "keyword_help", ("kh帮助", "鸣潮帮助")),
            keyword_login=cls._keywords(values, "keyword_login", ("kh登录", "鸣潮登录")),
            keyword_account=cls._keywords(values, "keyword_account", ("kh账号", "鸣潮账号")),
            keyword_account_info=cls._keywords(
                values, "keyword_account_info", ("kh账号信息", "鸣潮账号信息")
            ),
            keyword_character=cls._keywords(values, "keyword_character", ("kh角色", "鸣潮角色")),
            keyword_daily=cls._keywords(values, "keyword_daily", ("kh日常", "鸣潮日常")),
            keyword_exploration=cls._keywords(
                values, "keyword_exploration", ("kh探索", "鸣潮探索")
            ),
            allow_query_others=bool(values.get("allow_query_others", False)),
            login_link_ttl_minutes=cls._bounded_int(values, "login_link_ttl_minutes", 3, 1, 60),
            confirm_ttl_minutes=cls._bounded_int(values, "confirm_ttl_minutes", 5, 1, 30),
            confirm_max_attempts=cls._bounded_int(values, "confirm_max_attempts", 5, 1, 10),
            login_rate_window_minutes=cls._bounded_int(
                values, "login_rate_window_minutes", 10, 1, 60
            ),
            login_session_max_attempts=cls._bounded_int(
                values, "login_session_max_attempts", 5, 1, 20
            ),
            login_email_max_attempts=cls._bounded_int(values, "login_email_max_attempts", 8, 1, 30),
            login_ip_max_attempts=cls._bounded_int(values, "login_ip_max_attempts", 20, 1, 100),
            login_freeze_minutes=cls._bounded_int(values, "login_freeze_minutes", 15, 1, 120),
            auto_sync_enabled=bool(values.get("auto_sync_enabled", False)),
            auto_sync_interval_hours=cls._bounded_int(
                values, "auto_sync_interval_hours", 12, 6, 24 * 30
            ),
            sync_concurrency=cls._bounded_int(values, "sync_concurrency", 2, 1, 10),
            role_detail_concurrency=cls._bounded_int(values, "role_detail_concurrency", 2, 1, 5),
            request_timeout_seconds=cls._bounded_int(values, "request_timeout_seconds", 20, 5, 120),
            render_timeout_seconds=cls._bounded_int(values, "render_timeout_seconds", 30, 5, 120),
            admin_audit_retention_days=cls._bounded_int(
                values, "admin_audit_retention_days", 30, 0, 365
            ),
        )

    @staticmethod
    def _bounded_int(
        values: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int
    ) -> int:
        raw = values.get(key, default)
        if isinstance(raw, bool):
            raise SettingsError(f"配置 {key} 必须是整数")
        try:
            result = int(raw)
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"配置 {key} 必须是整数") from exc
        if not minimum <= result <= maximum:
            raise SettingsError(f"配置 {key} 必须在 {minimum} 到 {maximum} 之间")
        return result

    @staticmethod
    def _keywords(values: Mapping[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        raw = values.get(key, list(default))
        if not isinstance(raw, list):
            raise SettingsError(f"配置 {key} 必须是列表")
        result = tuple(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
        if not result:
            raise SettingsError(f"配置 {key} 至少需要一个关键词")
        return result

    @staticmethod
    def _normalized_roots(raw: Any) -> tuple[str, ...]:
        if not isinstance(raw, list):
            raise SettingsError("额外命令入口必须是列表")
        roots: list[str] = []
        normalized_seen: set[str] = set()
        for item in raw:
            root = str(item).strip()
            if not root:
                continue
            if not root.startswith("/"):
                root = f"/{root}"
            if not _ROOT_PATTERN.fullmatch(root):
                raise SettingsError(f"额外命令入口无效：{root}")
            folded = root.casefold()
            if folded not in normalized_seen:
                roots.append(root)
                normalized_seen.add(folded)
        return tuple(roots)

    @staticmethod
    def _networks(raw: Any) -> tuple[str, ...]:
        if not isinstance(raw, list):
            raise SettingsError("可信代理网段必须是列表")
        networks: list[str] = []
        for item in raw:
            value = str(item).strip()
            if not value:
                continue
            try:
                normalized = str(ipaddress.ip_network(value, strict=False))
            except ValueError as exc:
                raise SettingsError(f"可信代理网段无效：{value}") from exc
            if normalized not in networks:
                networks.append(normalized)
        return tuple(networks)
