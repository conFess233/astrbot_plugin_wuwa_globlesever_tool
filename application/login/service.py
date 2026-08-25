"""短期网页登录会话、复合账号绑定与登录限流。"""

import hmac
import json
import re
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from ...constants import PUBLIC_LOGIN_PREFIX
from ...domain.login import (
    AuthClient,
    AuthenticationError,
    AuthenticationUnavailableError,
    BrowserLoginState,
    BrowserSession,
    GuidePlayer,
    LoginCompletionResult,
    LoginLinkMessage,
    LoginSubmitResult,
)
from ...domain.models import RegionUid
from ...infrastructure.database import Database
from ...infrastructure.security import CryptoError, TokenCipher
from ..settings import PluginSettings

_EMAIL = re.compile(r"^[^\s@]{1,128}@[^\s@]{1,190}$")
_GEETEST_KEYS = {"captcha_output", "gen_time", "lot_number", "pass_token"}
_ACTIVE_STATUSES = ("active", "risk", "selecting")
_MAX_RATE_IDENTITIES = 10_000
_RateIdentity = tuple[str, str, int]


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
            db.execute("DELETE FROM pending_logins WHERE expires_at <= ?", (now,))
            db.execute("DELETE FROM pending_logins WHERE requesting_qq_id = ?", (qq_id,))
            db.execute(
                "INSERT INTO pending_logins (session_id, requesting_qq_id, origin_context, "
                "link_token_hash, status, failed_attempts, expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'created', 0, ?, ?, ?)",
                (session_id, qq_id, origin_context, digest, _iso(expires_at), now, now),
            )

        await self.database.write(operation)
        path = f"{PUBLIC_LOGIN_PREFIX}/login/{quote(link_token)}"
        return LoginLinkMessage(
            f"{self.settings.public_https_base_url}{path}",
            self.settings.login_link_ttl_minutes,
        )

    async def cancel(self, qq_id: str) -> bool:
        now = _iso()
        changed = await self.database.write(
            lambda db: (
                db.execute(
                    "UPDATE pending_logins SET status = 'cancelled', link_used_at = ?, "
                    "session_token_hash = NULL, csrf_token_hash = NULL, "
                    "encrypted_pending_tokens = NULL, updated_at = ? "
                    "WHERE requesting_qq_id = ? "
                    "AND status IN ('created', 'active', 'risk', 'selecting')",
                    (now, now, qq_id),
                ).rowcount
            )
        )
        return bool(changed)

    async def validate_link(self, link_token: str) -> bool:
        if not self._valid_token(link_token):
            return False
        digest = self._digest(f"login-link:{link_token}")
        row = await self.database.read(
            lambda db: db.execute(
                "SELECT 1 FROM pending_logins WHERE link_token_hash = ? "
                "AND status = 'created' AND link_exchanged_at IS NULL "
                "AND link_used_at IS NULL AND expires_at > ?",
                (digest, _iso()),
            ).fetchone()
        )
        return row is not None

    async def exchange_link(self, link_token: str, client_ip: str) -> BrowserSession:
        link_digest = self._digest(f"login-link:{link_token or 'invalid'}")
        ip_digest = self._digest(f"rate-ip:{client_ip or 'unknown'}")
        endpoint_digest = self._digest(f"rate-endpoint:exchange:{client_ip or 'unknown'}")
        identities = (
            ("link", link_digest, self.settings.login_session_max_attempts),
            ("ip:exchange", ip_digest, self.settings.login_ip_max_attempts),
            ("endpoint:exchange", endpoint_digest, self.settings.login_ip_max_attempts),
        )
        await self._check_rate_limits(None, identities)
        if not self._valid_token(link_token):
            await self._record_failure(None, identities)
            raise LoginSessionError("登录链接无效或已过期")

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
                "AND status = 'created' AND link_exchanged_at IS NULL "
                "AND link_used_at IS NULL AND expires_at > ?",
                (link_digest, now),
            ).fetchone()
            if row is None:
                raise LoginSessionError("登录链接无效或已过期")
            updated = db.execute(
                "UPDATE pending_logins SET status = 'active', session_token_hash = ?, "
                "csrf_token_hash = ?, link_exchanged_at = ?, encrypted_pending_tokens = ?, "
                "last_client_ip_hmac = ?, updated_at = ? "
                "WHERE link_token_hash = ? AND status = 'created' "
                "AND link_exchanged_at IS NULL AND link_used_at IS NULL",
                (
                    session_digest,
                    csrf_digest,
                    now,
                    encrypted,
                    ip_digest,
                    now,
                    link_digest,
                ),
            ).rowcount
            if updated != 1:
                raise LoginSessionError("登录链接无效或已过期")
            return datetime.fromisoformat(str(row["expires_at"]))

        try:
            expires_at = await self.database.write(operation)
        except LoginSessionError:
            await self._record_failure(None, identities)
            raise
        await self._clear_rate_limits(identities)
        return BrowserSession(session_token, csrf_token, expires_at)

    async def browser_state(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
    ) -> BrowserLoginState:
        row = await self._active_session(
            session_token,
            csrf_token,
            origin,
            statuses=_ACTIVE_STATUSES,
        )
        status = str(row["status"])
        players = ()
        if status == "selecting":
            players = self._players_from_json(
                str(row["available_accounts_json"] or row["available_uids_json"] or "[]")
            )
        return BrowserLoginState(
            status=status,
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            email_masked=str(row["email_masked"]) if row["email_masked"] else None,
            players=players,
        )

    async def submit_credentials(
        self,
        session_token: str,
        csrf_token: str,
        email: str,
        password: str,
        origin: str,
        client_ip: str,
        geetest: dict[str, str] | None = None,
        *,
        endpoint: str = "login",
    ) -> LoginSubmitResult:
        normalized_email = email.strip().casefold()
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
        endpoint_hmac = self._digest(f"rate-endpoint:{endpoint}:{client_ip or 'unknown'}")
        identities = (
            ("email", email_hmac, self.settings.login_email_max_attempts),
            ("ip:auth", ip_hmac, self.settings.login_ip_max_attempts),
            (f"endpoint:{endpoint}", endpoint_hmac, self.settings.login_ip_max_attempts),
            (
                "session",
                str(row["session_token_hash"]),
                self.settings.login_session_max_attempts,
            ),
        )
        await self._check_rate_limits(row, identities)
        if not _EMAIL.fullmatch(normalized_email):
            await self._record_failure(session_id, identities)
            raise LoginSessionError("邮箱格式无效")
        if not password or len(password) > 256:
            await self._record_failure(session_id, identities)
            raise LoginSessionError("密码不能为空且不能超过 256 个字符")
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
            await self._record_failure(session_id, identities)
            raise LoginSessionError(str(exc)) from exc

        players = self._unique_players(account.players)
        if not players:
            await self._record_failure(session_id, identities)
            raise LoginSessionError("该账号没有可绑定的国际服 UID")
        sensitive = account.sensitive_payload()
        sensitive["device_id"] = account.device_id
        encrypted = self.cipher.encrypt_text(json.dumps(sensitive, separators=(",", ":")))
        available = self._players_json(players)
        email_masked = self._mask_email(normalized_email)
        identity_hmac = self._digest(f"account-email:{normalized_email}")
        now = _iso()

        def operation(db: sqlite3.Connection) -> None:
            updated = db.execute(
                "UPDATE pending_logins SET status = 'selecting', encrypted_pending_tokens = ?, "
                "available_uids_json = ?, available_accounts_json = ?, "
                "email_identity_hmac = ?, email_masked = ?, failed_attempts = 0, "
                "locked_until = NULL, updated_at = ? "
                "WHERE session_id = ? AND status IN ('active', 'risk') AND expires_at > ?",
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

        await self.database.write(operation)
        await self._clear_rate_limits(identities)
        return LoginSubmitResult(False, players=players, email_masked=email_masked)

    async def complete_accounts(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        selected_accounts: list[dict[str, object]],
        default_account: dict[str, object],
    ) -> LoginCompletionResult:
        row = await self._active_session(
            session_token,
            csrf_token,
            origin,
            statuses=("selecting",),
        )
        available = self._players_from_json(
            str(row["available_accounts_json"] or row["available_uids_json"] or "[]")
        )
        player_map = {(player.region_id, player.uid): player for player in available}
        selected = tuple(dict.fromkeys(self._parse_account(item) for item in selected_accounts))
        default = self._parse_account(default_account)
        if not selected:
            raise LoginSessionError("至少选择一个本次账号返回的游戏账号")
        if default not in selected:
            raise LoginSessionError("默认账号必须位于已选账号中")
        if any((account.region_id, account.uid) not in player_map for account in selected):
            raise LoginSessionError("只能选择本次登录返回的游戏账号")
        session_hash = self._digest(f"login-session:{session_token}")
        csrf_hash = self._digest(f"login-csrf:{csrf_token}")
        now = _iso()

        def operation(db: sqlite3.Connection) -> LoginCompletionResult:
            current = db.execute(
                "SELECT * FROM pending_logins WHERE session_token_hash = ? "
                "AND status = 'selecting' AND expires_at > ?",
                (session_hash, now),
            ).fetchone()
            if current is None or not hmac.compare_digest(
                str(current["csrf_token_hash"] or ""), csrf_hash
            ):
                raise LoginSessionError("登录会话无效或已过期")
            return self._bind_completed(db, current, selected, default, player_map, now)

        return await self.database.write(operation)

    async def _active_session(
        self,
        session_token: str,
        csrf_token: str,
        origin: str,
        *,
        statuses: tuple[str, ...],
    ) -> sqlite3.Row:
        if origin != self.settings.public_https_base_url:
            raise LoginSessionError("登录页面来源校验失败")
        if not self._valid_token(session_token) or not self._valid_token(csrf_token):
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
        locked_until = row["locked_until"]
        if locked_until and datetime.fromisoformat(str(locked_until)) > _now():
            raise LoginSessionError("该登录会话尝试次数过多，请重新发起登录")
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
        row: sqlite3.Row | None,
        identities: tuple[_RateIdentity, ...],
    ) -> None:
        if (
            row is not None
            and int(row["failed_attempts"]) >= self.settings.login_session_max_attempts
        ):
            raise LoginSessionError("该登录会话尝试次数过多，请重新发起登录")
        now = _now()
        rate_rows = await self.database.read(
            lambda db: [
                db.execute(
                    "SELECT blocked_until FROM login_rate_limits WHERE scope = ? "
                    "AND identity_hmac = ?",
                    (scope, identity),
                ).fetchone()
                for scope, identity, _maximum in identities
            ]
        )
        for item in rate_rows:
            if item is None or not item["blocked_until"]:
                continue
            try:
                blocked = datetime.fromisoformat(str(item["blocked_until"]))
            except ValueError:
                blocked = now + timedelta(minutes=self.settings.login_freeze_minutes)
            if blocked > now:
                raise LoginSessionError("登录尝试过于频繁，请稍后重试")

    async def _record_failure(
        self,
        session_id: str | None,
        identities: tuple[_RateIdentity, ...],
    ) -> None:
        now = _now()
        window = timedelta(minutes=self.settings.login_rate_window_minutes)
        frozen_until = now + timedelta(minutes=self.settings.login_freeze_minutes)

        def operation(db: sqlite3.Connection) -> None:
            retention_minutes = (
                max(
                    self.settings.login_rate_window_minutes,
                    self.settings.login_freeze_minutes,
                )
                * 2
            )
            db.execute(
                "DELETE FROM login_rate_limits WHERE updated_at < ?",
                (_iso(now - timedelta(minutes=retention_minutes)),),
            )
            if session_id:
                row = db.execute(
                    "SELECT failed_attempts FROM pending_logins WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is not None:
                    attempts = int(row["failed_attempts"]) + 1
                    locked = attempts >= self.settings.login_session_max_attempts
                    db.execute(
                        "UPDATE pending_logins SET failed_attempts = ?, status = CASE "
                        "WHEN ? THEN 'locked' ELSE status END, locked_until = CASE "
                        "WHEN ? THEN ? ELSE locked_until END, updated_at = ? WHERE session_id = ?",
                        (
                            attempts,
                            locked,
                            locked,
                            _iso(frozen_until),
                            _iso(now),
                            session_id,
                        ),
                    )
            for scope, identity, maximum in identities:
                row = db.execute(
                    "SELECT attempts, window_started_at FROM login_rate_limits "
                    "WHERE scope = ? AND identity_hmac = ?",
                    (scope, identity),
                ).fetchone()
                if row is None:
                    attempts = 1
                    started = now
                else:
                    try:
                        started = datetime.fromisoformat(str(row["window_started_at"]))
                    except ValueError:
                        started = now
                    attempts = 1 if started + window <= now else int(row["attempts"]) + 1
                    if started + window <= now:
                        started = now
                blocked = _iso(frozen_until) if attempts >= maximum else None
                db.execute(
                    "INSERT INTO login_rate_limits (scope, identity_hmac, attempts, "
                    "window_started_at, blocked_until, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(scope, identity_hmac) DO UPDATE SET attempts = excluded.attempts, "
                    "window_started_at = excluded.window_started_at, "
                    "blocked_until = excluded.blocked_until, updated_at = excluded.updated_at",
                    (scope, identity, attempts, _iso(started), blocked, _iso(now)),
                )
            db.execute(
                "DELETE FROM login_rate_limits WHERE rowid IN ("
                "SELECT rowid FROM login_rate_limits ORDER BY updated_at DESC "
                f"LIMIT -1 OFFSET {_MAX_RATE_IDENTITIES})"
            )

        await self.database.write(operation)

    async def _clear_rate_limits(self, identities: tuple[_RateIdentity, ...]) -> None:
        def operation(db: sqlite3.Connection) -> None:
            for scope, identity, _maximum in identities:
                db.execute(
                    "DELETE FROM login_rate_limits WHERE scope = ? AND identity_hmac = ?",
                    (scope, identity),
                )

        await self.database.write(operation)

    def _bind_completed(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        selected_accounts: tuple[RegionUid, ...],
        default_account: RegionUid,
        player_map: dict[tuple[str, str], GuidePlayer],
        now: str,
    ) -> LoginCompletionResult:
        try:
            sensitive = json.loads(self.cipher.decrypt_text(str(row["encrypted_pending_tokens"])))
            device_id = str(sensitive.pop("device_id"))
            guide_status = str(sensitive.pop("guide_status", "unknown"))
        except (CryptoError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LoginSessionError("登录临时数据损坏，请重新发起登录") from exc
        if guide_status not in {"unknown", "valid", "needs_login", "invalid"}:
            guide_status = "unknown"
        qq_id = str(row["requesting_qq_id"])
        origin_context = str(row["origin_context"])
        identity_hmac = str(row["email_identity_hmac"] or "")
        email_masked = str(row["email_masked"] or "***")
        if not identity_hmac:
            raise LoginSessionError("登录账号标识缺失，请重新发起登录")
        credential = db.execute(
            "SELECT credential_id, qq_id FROM credentials WHERE account_identity_hmac = ?",
            (identity_hmac,),
        ).fetchone()
        if credential is not None and str(credential["qq_id"]) != qq_id:
            raise LoginConflictError("该国际服账号或游戏账号已绑定，无法重复绑定")
        for account in selected_accounts:
            owner = db.execute(
                "SELECT qq_id FROM game_accounts WHERE region_id = ? AND uid = ?",
                (account.region_id, account.uid),
            ).fetchone()
            if owner is not None and str(owner["qq_id"]) != qq_id:
                raise LoginConflictError("该国际服账号或游戏账号已绑定，无法重复绑定")

        db.execute(
            "INSERT INTO users (qq_id, last_origin_context, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(qq_id) DO UPDATE SET "
            "last_origin_context = excluded.last_origin_context, updated_at = excluded.updated_at",
            (qq_id, origin_context, now, now),
        )
        db.execute(
            "INSERT OR IGNORE INTO profiles "
            "(qq_id, profile_type, region_id, uid, updated_at) "
            "VALUES (?, 'local', NULL, NULL, ?)",
            (qq_id, now),
        )
        encrypted_tokens = self.cipher.encrypt_text(
            json.dumps(sensitive, ensure_ascii=False, separators=(",", ":"))
        )
        encrypted_device = self.cipher.encrypt_text(device_id)
        if credential is None:
            cursor = db.execute(
                "INSERT INTO credentials (qq_id, account_identity_hmac, email_masked, "
                "encrypted_tokens, encrypted_device_id, token_status, game_token_status, "
                "guide_token_status, last_success_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'valid', 'valid', ?, ?, ?)",
                (
                    qq_id,
                    identity_hmac,
                    email_masked,
                    encrypted_tokens,
                    encrypted_device,
                    guide_status,
                    now,
                    now,
                ),
            )
            credential_id = int(cursor.lastrowid)
        else:
            credential_id = int(credential["credential_id"])
            db.execute(
                "UPDATE credentials SET email_masked = ?, encrypted_tokens = ?, "
                "encrypted_device_id = ?, token_status = 'valid', game_token_status = 'valid', "
                "guide_token_status = ?, revoked_at = NULL, last_success_at = ?, "
                "updated_at = ? WHERE credential_id = ?",
                (
                    email_masked,
                    encrypted_tokens,
                    encrypted_device,
                    guide_status,
                    now,
                    now,
                    credential_id,
                ),
            )
        for account in selected_accounts:
            player = player_map[(account.region_id, account.uid)]
            db.execute(
                "INSERT INTO game_accounts (region_id, uid, qq_id, credential_id, region_name, "
                "player_name, sync_status, bound_at) VALUES (?, ?, ?, ?, ?, ?, 'never', ?) "
                "ON CONFLICT(region_id, uid) DO UPDATE SET "
                "credential_id = excluded.credential_id, region_name = excluded.region_name, "
                "player_name = excluded.player_name",
                (
                    account.region_id,
                    account.uid,
                    qq_id,
                    credential_id,
                    player.region_name,
                    player.player_name,
                    now,
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO profiles "
                "(qq_id, profile_type, region_id, uid, updated_at) "
                "VALUES (?, 'uid', ?, ?, ?)",
                (qq_id, account.region_id, account.uid, now),
            )
        db.execute(
            "DELETE FROM credentials WHERE qq_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM game_accounts WHERE game_accounts.credential_id = "
            "credentials.credential_id)",
            (qq_id,),
        )
        profile = db.execute(
            "SELECT profile_id FROM profiles WHERE qq_id = ? AND region_id = ? AND uid = ?",
            (qq_id, default_account.region_id, default_account.uid),
        ).fetchone()
        if profile is None:
            raise LoginSessionError("默认账号档案创建失败")
        db.execute(
            "UPDATE users SET default_region_id = ?, default_uid = ?, "
            "active_profile_id = ?, last_origin_context = ?, updated_at = ? WHERE qq_id = ?",
            (
                default_account.region_id,
                default_account.uid,
                int(profile["profile_id"]),
                origin_context,
                now,
                qq_id,
            ),
        )
        selected_payload = [
            {"region_id": account.region_id, "uid": account.uid} for account in selected_accounts
        ]
        db.execute(
            "UPDATE pending_logins SET status = 'completed', selected_accounts_json = ?, "
            "selected_uids_json = ?, selected_default_region_id = ?, "
            "selected_default_uid = ?, link_used_at = ?, completed_at = ?, "
            "session_token_hash = NULL, csrf_token_hash = NULL, "
            "encrypted_pending_tokens = NULL, available_uids_json = NULL, "
            "available_accounts_json = NULL, updated_at = ? WHERE session_id = ?",
            (
                json.dumps(selected_payload, separators=(",", ":")),
                json.dumps([account.uid for account in selected_accounts], separators=(",", ":")),
                default_account.region_id,
                default_account.uid,
                now,
                now,
                now,
                row["session_id"],
            ),
        )
        return LoginCompletionResult(
            qq_id=qq_id,
            origin_context=origin_context,
            email_masked=email_masked,
            selected_accounts=selected_accounts,
            default_account=default_account,
        )

    def _digest(self, value: str) -> str:
        return self.cipher.account_identity_hmac(value)

    @staticmethod
    def _valid_token(value: str) -> bool:
        return bool(value and len(value) <= 256 and re.fullmatch(r"[A-Za-z0-9_-]+", value))

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
    def _parse_account(raw: dict[str, object]) -> RegionUid:
        if not isinstance(raw, dict):
            raise LoginSessionError("游戏账号选择格式无效")
        region_id = str(raw.get("region_id") or "").strip()
        uid = str(raw.get("uid") or "").strip()
        if len(region_id) > 64 or len(uid) > 32 or not uid.isdigit():
            raise LoginSessionError("游戏账号选择格式无效")
        try:
            return RegionUid(region_id, uid)
        except ValueError as exc:
            raise LoginSessionError("游戏账号选择格式无效") from exc

    @staticmethod
    def _unique_players(players: tuple[GuidePlayer, ...]) -> tuple[GuidePlayer, ...]:
        return tuple({(player.region_id, player.uid): player for player in players}.values())

    @staticmethod
    def _players_json(players: tuple[GuidePlayer, ...]) -> str:
        return json.dumps(
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

    @staticmethod
    def _players_from_json(value: str) -> tuple[GuidePlayer, ...]:
        raw = json.loads(value)
        if not isinstance(raw, list):
            raise LoginSessionError("游戏账号数据格式无效")
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
