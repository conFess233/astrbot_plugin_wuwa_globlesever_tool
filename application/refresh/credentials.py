"""使用 SDK autoToken 安全续期游戏会话，并原子更新加密凭据。"""

import json
import sqlite3
from datetime import UTC, datetime

from ...domain.login import (
    AuthClient,
    AuthenticationError,
    AuthenticationUnavailableError,
    RefreshedSession,
)
from ...infrastructure.database import Database
from ...infrastructure.security import CryptoError, TokenCipher
from .coordinator import SingleFlightCoordinator


class CredentialRefreshError(RuntimeError):
    """表示 SDK 会话续期失败。"""


class CredentialRefreshAuthenticationError(CredentialRefreshError):
    """表示 autoToken 或本地续期凭据已失效。"""


class CredentialRefreshUnavailableError(CredentialRefreshError):
    """表示 SDK 续期接口暂时不可用。"""


class CredentialRefreshService:
    def __init__(self, database: Database, cipher: TokenCipher, client: AuthClient):
        self.database = database
        self.cipher = cipher
        self.client = client
        self._singleflight = SingleFlightCoordinator[RefreshedSession]()

    async def refresh(self, credential_id: int) -> RefreshedSession:
        return await self._singleflight.run(str(credential_id), lambda: self._run(credential_id))

    async def close(self) -> None:
        await self._singleflight.wait()

    async def _run(self, credential_id: int) -> RefreshedSession:
        row = await self.database.read(
            lambda db: db.execute(
                "SELECT encrypted_tokens, encrypted_device_id FROM credentials "
                "WHERE credential_id = ? AND revoked_at IS NULL",
                (credential_id,),
            ).fetchone()
        )
        if row is None:
            raise CredentialRefreshAuthenticationError("登录凭据不存在或已撤销")
        try:
            sensitive = json.loads(self.cipher.decrypt_text(str(row["encrypted_tokens"])))
            device_id = self.cipher.decrypt_text(str(row["encrypted_device_id"]))
        except (CryptoError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CredentialRefreshAuthenticationError("本地登录凭据无法读取") from exc
        if not isinstance(sensitive, dict):
            raise CredentialRefreshAuthenticationError("本地登录凭据格式无效")
        auto_token = str(sensitive.get("auto_token") or "").strip()
        if not auto_token or not device_id:
            raise CredentialRefreshAuthenticationError("当前登录没有可用的自动续期凭据")
        try:
            refreshed = await self.client.refresh_session(auto_token, device_id)
        except AuthenticationUnavailableError as exc:
            raise CredentialRefreshUnavailableError("国际服登录续期服务暂时不可用") from exc
        except AuthenticationError as exc:
            raise CredentialRefreshAuthenticationError("国际服自动登录状态已失效") from exc
        now = datetime.now(UTC).isoformat()

        def operation(db: sqlite3.Connection) -> None:
            current_row = db.execute(
                "SELECT encrypted_tokens FROM credentials "
                "WHERE credential_id = ? AND revoked_at IS NULL",
                (credential_id,),
            ).fetchone()
            if current_row is None:
                raise CredentialRefreshAuthenticationError("登录凭据不存在或已撤销")
            try:
                current = json.loads(self.cipher.decrypt_text(str(current_row["encrypted_tokens"])))
            except (CryptoError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CredentialRefreshAuthenticationError("本地登录凭据无法读取") from exc
            if not isinstance(current, dict):
                raise CredentialRefreshAuthenticationError("本地登录凭据格式无效")
            current.update(
                {
                    "auto_token": refreshed.auto_token,
                    "access_token": refreshed.access_token,
                    "oauth_code": refreshed.oauth_code,
                }
            )
            encrypted = self.cipher.encrypt_text(
                json.dumps(current, ensure_ascii=False, separators=(",", ":"))
            )
            updated = db.execute(
                "UPDATE credentials SET encrypted_tokens = ?, token_status = 'valid', "
                "game_token_status = 'valid', updated_at = ? "
                "WHERE credential_id = ? AND revoked_at IS NULL",
                (encrypted, now, credential_id),
            ).rowcount
            if updated != 1:
                raise CredentialRefreshAuthenticationError("登录凭据不存在或已撤销")

        await self.database.write(operation)
        return refreshed
