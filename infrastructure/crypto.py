"""插件凭据的主密钥管理和认证加密。"""

import base64
import binascii
import contextlib
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..constants import MASTER_KEY_ENV, PLUGIN_NAME

_KEY_BYTES = 32
_NONCE_BYTES = 12
_ENVELOPE_PREFIX = "v1."
_ASSOCIATED_DATA = f"{PLUGIN_NAME}:credentials:v1".encode()


class CryptoError(ValueError):
    """表示密钥或密文不符合插件约定。"""


class MasterKeyProvider:
    def __init__(self, secrets_dir: Path):
        self.key_path = secrets_dir / "master.key"

    def load_or_create(self) -> bytes:
        environment_key = os.getenv(MASTER_KEY_ENV, "").strip()
        if environment_key:
            return self._decode_environment_key(environment_key)

        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            return self._read_key_file()

        key = os.urandom(_KEY_BYTES)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            self._restrict_permissions()
            return key
        except BaseException:
            self.key_path.unlink(missing_ok=True)
            raise

    def _read_key_file(self) -> bytes:
        key = self.key_path.read_bytes()
        if len(key) != _KEY_BYTES:
            raise CryptoError("主密钥文件长度无效")
        self._restrict_permissions()
        return key

    @staticmethod
    def _decode_environment_key(value: str) -> bytes:
        try:
            padded = value + ("=" * (-len(value) % 4))
            key = base64.b64decode(padded.encode(), altchars=b"-_", validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise CryptoError("环境变量主密钥不是有效的 URL-safe Base64") from exc
        if len(key) != _KEY_BYTES:
            raise CryptoError("环境变量主密钥解码后必须为 32 字节")
        return key

    def _restrict_permissions(self) -> None:
        with contextlib.suppress(OSError):
            self.key_path.chmod(0o600)


class TokenCipher:
    def __init__(self, master_key: bytes):
        if len(master_key) != _KEY_BYTES:
            raise CryptoError("主密钥必须为 32 字节")
        self._key = master_key
        self._cipher = AESGCM(master_key)

    def encrypt_text(self, plaintext: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(nonce, plaintext.encode(), _ASSOCIATED_DATA)
        payload = base64.urlsafe_b64encode(nonce + ciphertext).decode()
        return f"{_ENVELOPE_PREFIX}{payload}"

    def decrypt_text(self, envelope: str) -> str:
        if not envelope.startswith(_ENVELOPE_PREFIX):
            raise CryptoError("不支持的密文版本")
        try:
            encoded = envelope.removeprefix(_ENVELOPE_PREFIX)
            padded = encoded + ("=" * (-len(encoded) % 4))
            payload = base64.b64decode(padded.encode(), altchars=b"-_", validate=True)
            nonce, ciphertext = payload[:_NONCE_BYTES], payload[_NONCE_BYTES:]
            if len(nonce) != _NONCE_BYTES or not ciphertext:
                raise ValueError("密文长度无效")
            plaintext = self._cipher.decrypt(nonce, ciphertext, _ASSOCIATED_DATA)
            return plaintext.decode()
        except (binascii.Error, InvalidTag, ValueError, UnicodeDecodeError) as exc:
            raise CryptoError("密文格式无效") from exc

    def account_identity_hmac(self, identity: str) -> str:
        normalized = identity.strip().casefold().encode()
        derived_key = hmac.new(self._key, b"account-identity-v1", hashlib.sha256).digest()
        return hmac.new(derived_key, normalized, hashlib.sha256).hexdigest()
