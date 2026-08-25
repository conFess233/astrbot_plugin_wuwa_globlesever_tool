"""短期网页登录会话、限流和 QQ 二次确认。"""

import hmac
import json
import re
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from ..constants import PUBLIC_LOGIN_PREFIX
from ..domain.login import (
    AuthClient,
    AuthenticationError,
    AuthenticationUnavailableError,
    BrowserSession,
    GuidePlayer,
    LoginConfirmationResult,
    LoginLinkMessage,
    LoginSelectionResult,
    LoginSubmitResult,
)
from ..infrastructure.crypto import CryptoError, TokenCipher
from ..infrastructure.database import Database
from .settings import PluginSettings

_EMAIL = re.compile(r"^[^\s@]{1,128}@[^\s@]{1,190}$")
_GEETEST_KEYS = {"captcha_output", "gen_time", "lot_number", "pass_token"}


class LoginSessionError(ValueError):
    """表示可安全展示给用户的登录会话错误。"""


class LoginConflictError(LoginSessionError):
    """表示账号或 UID 已被其他用户绑定，不泄露绑定方身份。"""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class LoginSessionService:
    def __init__(
        self,
        database: Database,
        cipher: TokenCipher,
        auth: AuthClient,
        settings: PluginSettings,
    ):
        self.database = database
        self.cipher = cipher
        self.auth = auth
        self.settings = settings

    async def create_link(self, qq_id: str, origin_context: str) -> LoginLinkMessage:
        if not self.settings.public_https_base_url:
            raise LoginSessionError("管理员尚未配置登录页公网 HTTPS 地址")
        if not origin_context:
            raise LoginSessionError("无法识别当前消息会话，请重新发送登录命令")
        session_id = uuid.uuid4().hex
        link_token = secrets.token_urlsafe(32)
        expires_at = _now() + timedelta(minutes=self.settings.login_link_ttl_minutes)
        digest = self._digest(f"login-link:{link_token}")
        now = _iso()

        def operation(db: sqlite3.Connection) -> None:
            db.execute("DELETE FROM pending_logins WHERE requesting_qq_id = ?", (qq_id,))
            db.execute(
                "INSERT INTO pending_logins (session_id, requesting_qq_id, origin_context, "
                "link_token_hash, status, failed_attempts, expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'created', 0, ?, ?, ?)",
                (session_id, qq_id, origin_context, digest, _iso(expires_at), now, now),
            )

        await self.database.write(operation)
        base = self.settings.public_https_base_url
        path = f"{PUBLIC_LOGIN_PREFIX}/login/{quote(link_token)}"
        return LoginLinkMessage(f"{base}{path}", self.settings.login_link_ttl_minutes)

    async def validate_link(self, link_token: str) -> bool:
        if not link_token or len(link_token) > 256:
            return False
        digest = self._digest(f"login-link:{link_token}")
        row = await self.database.read(
            lambda db: db.execute(
                "SELECT 1 FROM pending_logins WHERE link_token_hash = ? "
                "AND status = 'created' AND link_used_at IS NULL AND expires_at > ?",
                (digest, _iso()),
            ).fetchone()
        )
        return row is not None

    async def exchange_link(self, link_token: str) -> BrowserSession:
        if not link_token or len(link_token) > 256:
            raise LoginSessionError("登录链接无效或已过期")
        link_digest = self._digest(f"login-link:{link_token}")
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        session_digest = self._digest(f"login-session:{session_token}")
        csrf_digest = self._digest(f"login-csrf:{csrf_token}")
        encrypted = self.cipher.encrypt_text(
            json.dumps({"device_id": self.auth.new_device_id()}, separators=(",", ":"))
        )
        now = _iso()

        def operation(db: sqlite3.Connection) -> datetime:
            row = db.execute(
                "SELECT expires_at FROM pending_logins WHERE link_token_hash = ? "
                "AND status = 'created' AND link_used_at IS NULL AND expires_at > ?",
                (link_digest, now),
            ).fetchone()
            if row is None:
                raise LoginSessionError("登录链接无效或已过期")
            updated = db.execute(
                "UPDATE pending_logins SET status = 'active', session_token_hash = ?, "
                "csrf_token_hash = ?, link_used_at = ?, encrypted_pending_tokens = ?, "
                "updated_at = ? WHERE link_token_hash = ? AND link_used_at IS NULL",
                (session_digest, csrf_digest, now, encrypted, now, link_digest),
            ).rowcount
            if updated != 1:
                raise LoginSessionError("登录链接已被使用")
            return datetime.fromisoformat(str(row["expires_at"]))

        expires_at = await self.database.write(operation)
        return BrowserSession(session_token, csrf_token, expires_at)

    async def submit_credentials(
        self,
        session_token: str,
        csrf_token: str,
        email: str,
        password: str,
        origin: str,
        client_ip: str,
        geetest: dict[str, str] | None = None,
    ) -> LoginSubmitResult:
        normalized_email = email.strip().casefold()
        if not _EMAIL.fullmatch(normalized_email):
            raise LoginSessionError("邮箱格式无效")
        if not password or len(password) > 256:
            raise LoginSessionError("密码不能为空且不能超过 256 个字符")
        geetest = self._clean_geetest(geetest)
        row = await self._active_session(
            session_token,
            csrf_token,
            origin,
            statuses=("active", "risk"),
        )
        session_id = str(row["session_id"])
        email_hmac = self._digest(f"rate-email:{normalized_email}")
        ip_hmac = self._digest(f"rate-ip:{client_ip or 'unknown'}")
        await self._check_rate_limits(row, email_hmac, ip_hmac)
        try:
            pending = json.loads(self.cipher.decrypt_text(str(row["encrypted_pending_tokens"])))
            device_id = str(pending["device_id"])
        except (CryptoError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LoginSessionError("登录会话状态损坏，请重新发起登录") from exc

        try:
            sdk_result = await self.auth.email_login(
                normalized_email,
                password,
                device_id,
                geetest or None,
            )
            if sdk_result.risk_required:
                captcha_id = sdk_result.challenge.captcha_id if sdk_result.challenge else None
                await self._set_status(session_id, "risk")
                return LoginSubmitResult(True, captcha_id=captcha_id)
            account = await self.auth.complete_login(sdk_result, device_id)
        except AuthenticationUnavailableError as exc:
            raise LoginSessionError(str(exc)) from exc
        except AuthenticationError as exc:
            await self._record_failure(session_id, email_hmac, ip_hmac)
            raise LoginSessionError(str(exc)) from exc

        players = self._unique_players(account.players)
        if not players:
            raise LoginSessionError("该账号没有可绑定的国际服 UID")
        sensitive = account.sensitive_payload()
        sensitive["device_id"] = account.device_id
        encrypted = self.cipher.encrypt_text(json.dumps(sensitive, separators=(",", ":")))
        available = json.dumps(
            [
                {
                    "uid": player.uid,
                    "player_name": player.player_name,
                    "region_id": player.region_id,
                    "region_name": player.region_name,
                    "level": player.level,
                }
                for player in players
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        email_masked = self._mask_email(normalized_email)
        identity_hmac = self._digest(f"account-email:{normalized_email}")
        now = _iso()

        def operation(db: sqlite3.Connection) -> None:
            updated = db.execute(
                "UPDATE pending_logins SET status = 'selecting', encrypted_pending_tokens = ?, "
                "available_uids_json = ?, available_accounts_json = ?, "
                "email_identity_hmac = ?, email_masked = ?, "
                "updated_at = ? WHERE session_id = ? AND expires_at > ?",
                (
                    encrypted,
                    available,
                    available,
                    identity_hmac,
                    email_masked,
                    now,
                    session_id,
                    now,
                ),
            ).rowcount
            if updated != 1:
                raise LoginSessionError("登录会话已过期")
            db.execute(
                "DELETE FROM login_rate_limits WHERE (scope = 'email' AND identity_hmac = ?) "
                "OR (scope = 'ip' AND identity_hmac = ?)",
                (email_hmac, ip_hmac),
            )

        await self.database.write(operation)
        return LoginSubmitResult(False, players=players)

    async def select_uids(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        selected_uids: list[str],
        default_uid: str,
    ) -> LoginSelectionResult:
        row = await self._active_session(
            session_token,
            csrf_token,
            origin,
            statuses=("selecting",),
        )
        available = self._players_from_json(str(row["available_uids_json"] or "[]"))
        players_by_uid: dict[str, list[GuidePlayer]] = {}
        for player in available:
            players_by_uid.setdefault(player.uid, []).append(player)
        selected = tuple(
            dict.fromkeys(str(uid).strip() for uid in selected_uids if str(uid).strip())
        )
        if not selected or any(uid not in players_by_uid for uid in selected):
            raise LoginSessionError("至少选择一个本次账号返回的 UID")
        if any(len(players_by_uid[uid]) != 1 for uid in selected):
            raise LoginSessionError("相同 UID 存在于多个区服，请等待登录页区服选择升级")
        if default_uid not in selected:
            raise LoginSessionError("默认 UID 必须位于已选 UID 中")
        selected_accounts = tuple(
            {
                "region_id": players_by_uid[uid][0].region_id,
                "uid": uid,
            }
            for uid in selected
        )
        default_region_id = players_by_uid[default_uid][0].region_id
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        expires_at = _now() + timedelta(minutes=self.settings.confirm_ttl_minutes)
        code_hash = self._digest(f"login-confirm:{row['session_id']}:{code}")
        now = _iso()

        def operation(db: sqlite3.Connection) -> None:
            updated = db.execute(
                "UPDATE pending_logins SET status = 'awaiting_confirm', selected_uids_json = ?, "
                "selected_accounts_json = ?, selected_default_uid = ?, "
                "selected_default_region_id = ?, confirm_code_hash = ?, failed_attempts = 0, "
                "expires_at = ?, updated_at = ? WHERE session_id = ? AND status = 'selecting'",
                (
                    json.dumps(selected, separators=(",", ":")),
                    json.dumps(selected_accounts, separators=(",", ":")),
                    default_uid,
                    default_region_id,
                    code_hash,
                    _iso(expires_at),
                    now,
                    row["session_id"],
                ),
            ).rowcount
            if updated != 1:
                raise LoginSessionError("登录会话状态已变化，请重新发起登录")

        await self.database.write(operation)
        return LoginSelectionResult(code, expires_at)

    async def confirm_login(
        self,
        qq_id: str,
        origin_context: str,
        code: str,
    ) -> LoginConfirmationResult:
        if not code.isdigit() or len(code) != 6:
            raise LoginSessionError("登录确认码无效或已过期")
        now = _iso()

        def operation(db: sqlite3.Connection) -> LoginConfirmationResult | None:
            row = db.execute(
                "SELECT * FROM pending_logins WHERE requesting_qq_id = ? "
                "AND origin_context = ? AND status = 'awaiting_confirm' AND expires_at > ? "
                "ORDER BY created_at DESC LIMIT 1",
                (qq_id, origin_context, now),
            ).fetchone()
            if row is None:
                raise LoginSessionError("登录确认码无效或已过期")
            expected = str(row["confirm_code_hash"] or "")
            actual = self._digest(f"login-confirm:{row['session_id']}:{code}")
            if not hmac.compare_digest(expected, actual):
                attempts = int(row["failed_attempts"]) + 1
                status = (
                    "invalid"
                    if attempts >= self.settings.confirm_max_attempts
                    else "awaiting_confirm"
                )
                db.execute(
                    "UPDATE pending_logins SET failed_attempts = ?, status = ?, updated_at = ? "
                    "WHERE session_id = ?",
                    (attempts, status, now, row["session_id"]),
                )
                return None
            return self._bind_confirmed(db, row, qq_id, now)

        result = await self.database.write(operation)
        if result is None:
            raise LoginSessionError("登录确认码无效或已过期")
        return result

    async def _active_session(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        *,
        statuses: tuple[str, ...] = ("active", "risk", "selecting"),
    ) -> sqlite3.Row:
        if not session_token or not csrf_token:
            raise LoginSessionError("登录会话无效或已过期")
        session_hash = self._digest(f"login-session:{session_token}")
        csrf_hash = self._digest(f"login-csrf:{csrf_token}")
        placeholders = ",".join("?" for _ in statuses)
        row = await self.database.read(
            lambda db: db.execute(
                f"SELECT * FROM pending_logins WHERE session_token_hash = ? "
                f"AND status IN ({placeholders}) AND expires_at > ?",
                (session_hash, *statuses, _iso()),
            ).fetchone()
        )
        if row is None or not hmac.compare_digest(str(row["csrf_token_hash"] or ""), csrf_hash):
            raise LoginSessionError("登录会话无效或已过期")
        if origin != self.settings.public_https_base_url:
            raise LoginSessionError("登录页面来源校验失败")
        return row

    async def _set_status(self, session_id: str, status: str) -> None:
        await self.database.write(
            lambda db: db.execute(
                "UPDATE pending_logins SET status = ?, updated_at = ? WHERE session_id = ?",
                (status, _iso(), session_id),
            )
        )

    async def _check_rate_limits(
        self,
        row: sqlite3.Row,
        email_hmac: str,
        ip_hmac: str,
    ) -> None:
        if int(row["failed_attempts"]) >= self.settings.login_session_max_attempts:
            raise LoginSessionError("该登录会话尝试次数过多，请重新发起登录")
        now = _now()
        limits = (("email", email_hmac), ("ip", ip_hmac))
        rows = await self.database.read(
            lambda db: [
                db.execute(
                    "SELECT blocked_until FROM login_rate_limits WHERE scope = ? "
                    "AND identity_hmac = ?",
                    identity,
                ).fetchone()
                for identity in limits
            ]
        )
        if any(
            item is not None
            and item["blocked_until"]
            and datetime.fromisoformat(str(item["blocked_until"])) > now
            for item in rows
        ):
            raise LoginSessionError("登录尝试过于频繁，请稍后重试")

    async def _record_failure(self, session_id: str, email_hmac: str, ip_hmac: str) -> None:
        now = _now()
        window = timedelta(minutes=self.settings.login_rate_window_minutes)
        frozen_until = now + timedelta(minutes=self.settings.login_freeze_minutes)

        def operation(db: sqlite3.Connection) -> None:
            db.execute(
                "UPDATE pending_logins SET failed_attempts = failed_attempts + 1, updated_at = ? "
                "WHERE session_id = ?",
                (_iso(now), session_id),
            )
            for scope, identity, maximum in (
                ("email", email_hmac, self.settings.login_email_max_attempts),
                ("ip", ip_hmac, self.settings.login_ip_max_attempts),
            ):
                row = db.execute(
                    "SELECT attempts, window_started_at FROM login_rate_limits "
                    "WHERE scope = ? AND identity_hmac = ?",
                    (scope, identity),
                ).fetchone()
                if (
                    row is None
                    or datetime.fromisoformat(str(row["window_started_at"])) + window <= now
                ):
                    attempts = 1
                    started = now
                else:
                    attempts = int(row["attempts"]) + 1
                    started = datetime.fromisoformat(str(row["window_started_at"]))
                blocked = _iso(frozen_until) if attempts >= maximum else None
                db.execute(
                    "INSERT INTO login_rate_limits (scope, identity_hmac, attempts, "
                    "window_started_at, blocked_until, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(scope, identity_hmac) DO UPDATE SET attempts = excluded.attempts, "
                    "window_started_at = excluded.window_started_at, "
                    "blocked_until = excluded.blocked_until, updated_at = excluded.updated_at",
                    (scope, identity, attempts, _iso(started), blocked, _iso(now)),
                )

        await self.database.write(operation)

    def _bind_confirmed(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        qq_id: str,
        now: str,
    ) -> LoginConfirmationResult:
        try:
            sensitive = json.loads(self.cipher.decrypt_text(str(row["encrypted_pending_tokens"])))
            selected = tuple(json.loads(str(row["selected_uids_json"])))
            available = self._players_from_json(str(row["available_uids_json"]))
            selected_accounts_raw = json.loads(str(row["selected_accounts_json"] or "[]"))
            device_id = str(sensitive.pop("device_id"))
        except (CryptoError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LoginSessionError("待确认登录数据损坏，请重新发起登录") from exc
        default_uid = str(row["selected_default_uid"] or "")
        default_region_id = str(row["selected_default_region_id"] or "")
        player_map = {(player.region_id, player.uid): player for player in available}
        if selected_accounts_raw:
            selected_accounts = tuple(
                (str(item["region_id"]), str(item["uid"]))
                for item in selected_accounts_raw
                if isinstance(item, dict)
            )
        else:
            # 兼容迁移前已进入确认阶段的登录会话；重复 UID 无法安全推断区服。
            players_by_uid: dict[str, list[GuidePlayer]] = {}
            for player in available:
                players_by_uid.setdefault(player.uid, []).append(player)
            if any(len(players_by_uid.get(uid, ())) != 1 for uid in selected):
                raise LoginSessionError("待确认 UID 区服不明确，请重新发起登录")
            selected_accounts = tuple((players_by_uid[uid][0].region_id, uid) for uid in selected)
            if default_uid in players_by_uid and len(players_by_uid[default_uid]) == 1:
                default_region_id = players_by_uid[default_uid][0].region_id
        if (
            not selected_accounts
            or (default_region_id, default_uid) not in selected_accounts
            or any(account not in player_map for account in selected_accounts)
        ):
            raise LoginSessionError("待确认 UID 数据无效，请重新发起登录")
        identity_hmac = str(row["email_identity_hmac"] or "")
        email_masked = str(row["email_masked"] or "***")
        credential = db.execute(
            "SELECT credential_id, qq_id FROM credentials WHERE account_identity_hmac = ?",
            (identity_hmac,),
        ).fetchone()
        if credential is not None and str(credential["qq_id"]) != qq_id:
            raise LoginConflictError("该国际服账号或 UID 已绑定，无法重复绑定")
        for region_id, uid in selected_accounts:
            owner = db.execute(
                "SELECT qq_id FROM game_accounts WHERE region_id = ? AND uid = ?",
                (region_id, uid),
            ).fetchone()
            if owner is not None and str(owner["qq_id"]) != qq_id:
                raise LoginConflictError("该国际服账号或 UID 已绑定，无法重复绑定")

        db.execute(
            "INSERT INTO users (qq_id, created_at, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(qq_id) DO UPDATE SET updated_at = excluded.updated_at",
            (qq_id, now, now),
        )
        db.execute(
            "INSERT OR IGNORE INTO profiles (qq_id, profile_type, uid, updated_at) "
            "VALUES (?, 'local', NULL, ?)",
            (qq_id, now),
        )
        encrypted_tokens = self.cipher.encrypt_text(
            json.dumps(sensitive, ensure_ascii=False, separators=(",", ":"))
        )
        encrypted_device = self.cipher.encrypt_text(device_id)
        if credential is None:
            cursor = db.execute(
                "INSERT INTO credentials (qq_id, account_identity_hmac, email_masked, "
                "encrypted_tokens, encrypted_device_id, token_status, last_success_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'valid', ?, ?)",
                (
                    qq_id,
                    identity_hmac,
                    email_masked,
                    encrypted_tokens,
                    encrypted_device,
                    now,
                    now,
                ),
            )
            credential_id = int(cursor.lastrowid)
        else:
            credential_id = int(credential["credential_id"])
            db.execute(
                "UPDATE credentials SET email_masked = ?, encrypted_tokens = ?, "
                "encrypted_device_id = ?, token_status = 'valid', last_success_at = ?, "
                "updated_at = ? WHERE credential_id = ?",
                (email_masked, encrypted_tokens, encrypted_device, now, now, credential_id),
            )
        for region_id, uid in selected_accounts:
            player = player_map[(region_id, uid)]
            db.execute(
                "INSERT INTO game_accounts (region_id, uid, qq_id, credential_id, region_name, "
                "player_name, sync_status) VALUES (?, ?, ?, ?, ?, ?, 'never') "
                "ON CONFLICT(region_id, uid) DO UPDATE SET "
                "credential_id = excluded.credential_id, region_name = excluded.region_name, "
                "player_name = excluded.player_name",
                (
                    region_id,
                    uid,
                    qq_id,
                    credential_id,
                    player.region_name,
                    player.player_name,
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO profiles "
                "(qq_id, profile_type, region_id, uid, updated_at) "
                "VALUES (?, 'uid', ?, ?, ?)",
                (qq_id, region_id, uid, now),
            )
        db.execute(
            "DELETE FROM credentials WHERE qq_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM game_accounts WHERE game_accounts.credential_id = "
            "credentials.credential_id)",
            (qq_id,),
        )
        profile = db.execute(
            "SELECT profile_id FROM profiles WHERE qq_id = ? AND region_id = ? AND uid = ?",
            (qq_id, default_region_id, default_uid),
        ).fetchone()
        db.execute(
            "UPDATE users SET default_region_id = ?, default_uid = ?, "
            "active_profile_id = ?, updated_at = ? "
            "WHERE qq_id = ?",
            (default_region_id, default_uid, int(profile["profile_id"]), now, qq_id),
        )
        db.execute("DELETE FROM pending_logins WHERE session_id = ?", (row["session_id"],))
        return LoginConfirmationResult(email_masked, selected, default_uid)

    def _digest(self, value: str) -> str:
        return self.cipher.account_identity_hmac(value)

    @staticmethod
    def _clean_geetest(raw: dict[str, str] | None) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        return {
            key: str(raw.get(key, "")).strip()[:4096]
            for key in _GEETEST_KEYS
            if str(raw.get(key, "")).strip()
        }

    @staticmethod
    def _unique_players(players: tuple[GuidePlayer, ...]) -> tuple[GuidePlayer, ...]:
        return tuple({(player.region_id, player.uid): player for player in players}.values())

    @staticmethod
    def _players_from_json(value: str) -> tuple[GuidePlayer, ...]:
        raw = json.loads(value)
        if not isinstance(raw, list):
            raise LoginSessionError("UID 数据格式无效")
        return tuple(
            GuidePlayer(
                uid=str(item["uid"]),
                player_name=str(item["player_name"]) if item.get("player_name") else None,
                region_id=str(item["region_id"]),
                region_name=str(item["region_name"]),
                level=int(item["level"]) if item.get("level") is not None else None,
            )
            for item in raw
            if isinstance(item, dict)
        )

    @staticmethod
    def _mask_email(email: str) -> str:
        local, domain = email.split("@", 1)
        visible = local[:2] if len(local) > 2 else local[:1]
        return f"{visible}{'*' * max(3, len(local) - len(visible))}@{domain}"
