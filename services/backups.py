"""Dashboard ZIP 备份导出、预检与合并恢复。"""

import asyncio
import hashlib
import json
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ..constants import PLUGIN_NAME, PLUGIN_VERSION, SCHEMA_VERSION
from ..infrastructure.crypto import CryptoError, TokenCipher
from ..infrastructure.database import Database
from ..infrastructure.storage import RuntimePaths
from .catalog import CharacterCatalog
from .settings import PluginSettings

_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_ENTRY_BYTES = 96 * 1024 * 1024
_MAX_ENTRIES = 10
_DATABASE_ENTRY = "database.sqlite3"
_CONFIG_ENTRY = "config.json"
_CATALOG_ENTRY = "static/characters.json"
_MANIFEST_ENTRY = "manifest.json"


class BackupError(ValueError):
    """表示备份文件不安全、不完整或与当前实例不兼容。"""


@dataclass(frozen=True, slots=True)
class BackupInspection:
    schema_version: int
    plugin_version: str
    includes_encrypted_credentials: bool
    credential_count: int
    invalid_credentials: int
    users: int
    accounts: int
    characters: int
    config: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plugin_version": self.plugin_version,
            "includes_encrypted_credentials": self.includes_encrypted_credentials,
            "credential_count": self.credential_count,
            "invalid_credentials": self.invalid_credentials,
            "users": self.users,
            "accounts": self.accounts,
            "characters": self.characters,
            "config": self.config,
        }


class BackupService:
    def __init__(self, database: Database, paths: RuntimePaths, cipher: TokenCipher):
        self.database = database
        self.paths = paths
        self.cipher = cipher

    async def export(
        self,
        config: dict[str, object],
        *,
        include_encrypted_credentials: bool,
    ) -> Path:
        await asyncio.to_thread(self.paths.backups.mkdir, parents=True, exist_ok=True)
        target = self.paths.backups / (
            f"wuwa-backup-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.zip"
        )
        await asyncio.to_thread(
            self._export_sync,
            target,
            config,
            include_encrypted_credentials,
        )
        return target

    async def inspect(self, archive: Path) -> BackupInspection:
        try:
            return await asyncio.to_thread(self._inspect_sync, archive)
        except BackupError:
            raise
        except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
            raise BackupError("备份 ZIP 无法安全读取") from exc

    async def restore(
        self,
        archive: Path,
        *,
        mode: str,
        restore_credentials: bool,
    ) -> dict[str, int]:
        if mode not in {"preserve", "overwrite"}:
            raise BackupError("恢复模式必须是 preserve 或 overwrite")
        temporary_root: Path | None = None
        try:
            inspection = await self.inspect(archive)
            if restore_credentials and inspection.invalid_credentials:
                raise BackupError("备份中的加密凭据无法使用当前主密钥解密")
            source_path, temporary_root = await asyncio.to_thread(self._extract_database, archive)
            source_database = Database(source_path)
            await source_database.initialize()
            await source_database.close()
            return await self.database.write(
                lambda target: self._merge(
                    target,
                    source_path,
                    mode=mode,
                    restore_credentials=restore_credentials,
                )
            )
        except BackupError:
            raise
        except (KeyError, OSError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
            raise BackupError("备份数据库无法安全恢复") from exc
        finally:
            if temporary_root is not None:
                await asyncio.to_thread(shutil.rmtree, temporary_root, ignore_errors=True)

    async def catalog_payload(self, archive: Path) -> dict[str, object]:
        def operation() -> dict[str, object]:
            with zipfile.ZipFile(archive) as source:
                payload = _json_from_zip(source, _CATALOG_ENTRY)
            CharacterCatalog.from_payload(payload)
            return payload

        try:
            return await asyncio.to_thread(operation)
        except BackupError:
            raise
        except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
            raise BackupError("备份角色目录无法读取") from exc

    def _export_sync(
        self,
        target: Path,
        config: dict[str, object],
        include_encrypted_credentials: bool,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=self.paths.backups) as temporary:
            root = Path(temporary)
            database_copy = root / _DATABASE_ENTRY
            source = sqlite3.connect(self.database.path)
            destination = sqlite3.connect(database_copy)
            try:
                source.backup(destination)
                destination.execute("PRAGMA foreign_keys = ON")
                with destination:
                    destination.execute("DELETE FROM pending_logins")
                    destination.execute("DELETE FROM pending_actions")
                    destination.execute("DELETE FROM login_rate_limits")
                    if not include_encrypted_credentials:
                        destination.execute(
                            "UPDATE credentials SET encrypted_tokens = '', "
                            "encrypted_device_id = '', token_status = 'needs_login'"
                        )
            finally:
                destination.close()
                source.close()

            portable = {
                key: value
                for key, value in config.items()
                if key in PluginSettings.__dataclass_fields__
            }
            config_path = root / _CONFIG_ENTRY
            config_path.write_text(
                json.dumps(portable, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            catalog_source = self.paths.cache_static_data / "characters.json"
            if not catalog_source.is_file():
                catalog_source = (
                    Path(__file__).resolve().parent.parent / "assets" / "static" / "characters.json"
                )
            catalog_path = root / _CATALOG_ENTRY
            catalog_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(catalog_source, catalog_path)

            files = (_DATABASE_ENTRY, _CONFIG_ENTRY, _CATALOG_ENTRY)
            manifest = {
                "plugin": PLUGIN_NAME,
                "plugin_version": PLUGIN_VERSION,
                "schema_version": SCHEMA_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "includes_encrypted_credentials": include_encrypted_credentials,
                "files": {name: _sha256(root / name) for name in files},
            }
            (root / _MANIFEST_ENTRY).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name in (*files, _MANIFEST_ENTRY):
                    archive.write(root / name, name)

    def _inspect_sync(self, archive: Path) -> BackupInspection:
        if not archive.is_file() or archive.stat().st_size > _MAX_ARCHIVE_BYTES:
            raise BackupError("备份不存在或超过 128 MiB")
        with zipfile.ZipFile(archive) as source:
            infos = source.infolist()
            if (
                not infos
                or len(infos) > _MAX_ENTRIES
                or sum(info.file_size for info in infos) > _MAX_ARCHIVE_BYTES
            ):
                raise BackupError("备份文件数量无效")
            names = {info.filename for info in infos}
            required = {_DATABASE_ENTRY, _CONFIG_ENTRY, _CATALOG_ENTRY, _MANIFEST_ENTRY}
            if len(names) != len(infos) or names != required:
                raise BackupError("备份条目必须与清单完全一致且不能重复")
            for info in infos:
                _validate_zip_entry(info)
            manifest = _json_from_zip(source, _MANIFEST_ENTRY)
            if manifest.get("plugin") != PLUGIN_NAME:
                raise BackupError("备份不属于本插件")
            schema = int(manifest.get("schema_version", -1))
            if schema < 1 or schema > SCHEMA_VERSION:
                raise BackupError("备份数据库版本不受支持")
            hashes = manifest.get("files")
            if not isinstance(hashes, dict):
                raise BackupError("备份哈希清单无效")
            for name in (_DATABASE_ENTRY, _CONFIG_ENTRY, _CATALOG_ENTRY):
                actual = hashlib.sha256(source.read(name)).hexdigest()
                if hashes.get(name) != actual:
                    raise BackupError(f"备份文件校验失败：{name}")
            config = _json_from_zip(source, _CONFIG_ENTRY)
            PluginSettings.from_mapping(config)
            CharacterCatalog.from_payload(_json_from_zip(source, _CATALOG_ENTRY))

        database_path, temporary_root = self._extract_database(archive)
        try:
            db = sqlite3.connect(database_path)
            db.row_factory = sqlite3.Row
            try:
                integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    raise BackupError("备份数据库完整性检查失败")
                db_schema = int(db.execute("PRAGMA user_version").fetchone()[0])
                if db_schema != schema:
                    raise BackupError("manifest 与数据库 schema 版本不一致")
                counts = {
                    name: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for name, table in (
                        ("users", "users"),
                        ("accounts", "game_accounts"),
                        ("characters", "characters"),
                        ("credentials", "credentials"),
                    )
                }
                invalid = 0
                encrypted = 0
                for row in db.execute(
                    "SELECT encrypted_tokens, encrypted_device_id FROM credentials"
                ):
                    values = (
                        str(row["encrypted_tokens"] or ""),
                        str(row["encrypted_device_id"] or ""),
                    )
                    if not any(values):
                        continue
                    encrypted += 1
                    try:
                        for value in values:
                            if value:
                                self.cipher.decrypt_text(value)
                    except CryptoError:
                        invalid += 1
            finally:
                db.close()
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)
        return BackupInspection(
            schema_version=schema,
            plugin_version=str(manifest.get("plugin_version") or ""),
            includes_encrypted_credentials=bool(manifest.get("includes_encrypted_credentials"))
            and encrypted > 0,
            credential_count=counts["credentials"],
            invalid_credentials=invalid,
            users=counts["users"],
            accounts=counts["accounts"],
            characters=counts["characters"],
            config=config,
        )

    @staticmethod
    def _extract_database(archive: Path) -> tuple[Path, Path]:
        root = Path(tempfile.mkdtemp(prefix="wuwa-backup-"))
        database_path = root / _DATABASE_ENTRY
        try:
            with zipfile.ZipFile(archive) as source:
                info = source.getinfo(_DATABASE_ENTRY)
                _validate_zip_entry(info)
                with source.open(info) as incoming, database_path.open("wb") as output:
                    shutil.copyfileobj(incoming, output, length=64 * 1024)
            return database_path, root
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

    def _merge(
        self,
        target: sqlite3.Connection,
        source_path: Path,
        *,
        mode: str,
        restore_credentials: bool,
    ) -> dict[str, int]:
        source = sqlite3.connect(source_path)
        source.row_factory = sqlite3.Row
        counts = {
            "users": 0,
            "credentials": 0,
            "accounts": 0,
            "snapshots": 0,
            "characters": 0,
            "skipped": 0,
        }
        credential_map: dict[int, int] = {}
        profile_map: dict[int, int] = {}
        try:
            for row in source.execute("SELECT * FROM users ORDER BY qq_id"):
                exists = target.execute(
                    "SELECT 1 FROM users WHERE qq_id = ?", (row["qq_id"],)
                ).fetchone()
                if exists is None:
                    target.execute(
                        "INSERT INTO users (qq_id, language, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (row["qq_id"], row["language"], row["created_at"], _iso()),
                    )
                    counts["users"] += 1
                elif mode == "overwrite":
                    target.execute(
                        "UPDATE users SET language = ?, updated_at = ? WHERE qq_id = ?",
                        (row["language"], _iso(), row["qq_id"]),
                    )

            for row in source.execute("SELECT * FROM credentials ORDER BY credential_id"):
                existing = target.execute(
                    "SELECT credential_id, qq_id FROM credentials WHERE account_identity_hmac = ?",
                    (row["account_identity_hmac"],),
                ).fetchone()
                if existing is not None and str(existing["qq_id"]) != str(row["qq_id"]):
                    counts["skipped"] += 1
                    continue
                encrypted_tokens = str(row["encrypted_tokens"] or "") if restore_credentials else ""
                encrypted_device = (
                    str(row["encrypted_device_id"] or "") if restore_credentials else ""
                )
                token_status = row["token_status"] if restore_credentials else "needs_login"
                if existing is None:
                    cursor = target.execute(
                        "INSERT INTO credentials (qq_id, account_identity_hmac, email_masked, "
                        "encrypted_tokens, encrypted_device_id, token_status, last_success_at, "
                        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            row["qq_id"],
                            row["account_identity_hmac"],
                            row["email_masked"],
                            encrypted_tokens,
                            encrypted_device,
                            token_status,
                            row["last_success_at"] if restore_credentials else None,
                            _iso(),
                        ),
                    )
                    destination_id = int(cursor.lastrowid)
                    counts["credentials"] += 1
                else:
                    destination_id = int(existing["credential_id"])
                    if mode == "overwrite" and restore_credentials:
                        target.execute(
                            "UPDATE credentials SET email_masked = ?, encrypted_tokens = ?, "
                            "encrypted_device_id = ?, token_status = ?, last_success_at = ?, "
                            "updated_at = ? WHERE credential_id = ?",
                            (
                                row["email_masked"],
                                encrypted_tokens,
                                encrypted_device,
                                token_status,
                                row["last_success_at"],
                                _iso(),
                                destination_id,
                            ),
                        )
                credential_map[int(row["credential_id"])] = destination_id

            for row in source.execute("SELECT * FROM game_accounts ORDER BY region_id, uid"):
                existing = target.execute(
                    "SELECT qq_id FROM game_accounts WHERE region_id = ? AND uid = ?",
                    (row["region_id"], row["uid"]),
                ).fetchone()
                if existing is not None and str(existing["qq_id"]) != str(row["qq_id"]):
                    counts["skipped"] += 1
                    continue
                credential_id = credential_map.get(int(row["credential_id"]))
                if credential_id is None:
                    counts["skipped"] += 1
                    continue
                values = (
                    row["qq_id"],
                    credential_id,
                    row["region_id"],
                    row["region_name"],
                    row["player_name"],
                    row["sync_status"] if restore_credentials else "needs_login",
                    row["last_sync_attempt_at"],
                    row["last_sync_success_at"],
                    row["last_error_category"],
                    row["uid"],
                )
                if existing is None:
                    target.execute(
                        "INSERT INTO game_accounts (qq_id, credential_id, region_id, region_name, "
                        "player_name, sync_status, last_sync_attempt_at, last_sync_success_at, "
                        "last_error_category, uid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        values,
                    )
                    counts["accounts"] += 1
                elif mode == "overwrite":
                    target.execute(
                        "UPDATE game_accounts SET qq_id = ?, credential_id = ?, region_id = ?, "
                        "region_name = ?, player_name = ?, sync_status = ?, "
                        "last_sync_attempt_at = ?, last_sync_success_at = ?, "
                        "last_error_category = ? WHERE region_id = ? AND uid = ?",
                        (*values[:-1], row["region_id"], row["uid"]),
                    )

            for row in source.execute("SELECT * FROM profiles ORDER BY profile_id"):
                if (
                    row["profile_type"] == "uid"
                    and target.execute(
                        "SELECT 1 FROM game_accounts WHERE region_id = ? AND uid = ?",
                        (row["region_id"], row["uid"]),
                    ).fetchone()
                    is None
                ):
                    continue
                existing = target.execute(
                    "SELECT profile_id FROM profiles WHERE qq_id = ? AND profile_type = ? "
                    "AND ((uid IS NULL AND ? IS NULL) "
                    "OR (region_id = ? AND uid = ?))",
                    (
                        row["qq_id"],
                        row["profile_type"],
                        row["uid"],
                        row["region_id"],
                        row["uid"],
                    ),
                ).fetchone()
                if existing is None:
                    cursor = target.execute(
                        "INSERT INTO profiles "
                        "(qq_id, profile_type, region_id, uid, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            row["qq_id"],
                            row["profile_type"],
                            row["region_id"],
                            row["uid"],
                            _iso(),
                        ),
                    )
                    destination_id = int(cursor.lastrowid)
                else:
                    destination_id = int(existing["profile_id"])
                profile_map[int(row["profile_id"])] = destination_id

            for row in source.execute(
                "SELECT qq_id, default_region_id, default_uid, active_profile_id FROM users"
            ):
                current = target.execute(
                    "SELECT default_region_id, default_uid, active_profile_id "
                    "FROM users WHERE qq_id = ?",
                    (row["qq_id"],),
                ).fetchone()
                if current is None:
                    continue
                default_region_id = row["default_region_id"]
                default_uid = row["default_uid"]
                if (
                    default_uid
                    and target.execute(
                        "SELECT 1 FROM game_accounts WHERE region_id = ? AND uid = ? AND qq_id = ?",
                        (default_region_id, default_uid, row["qq_id"]),
                    ).fetchone()
                    is None
                ):
                    default_region_id = None
                    default_uid = None
                active_profile_id = (
                    profile_map.get(int(row["active_profile_id"]))
                    if row["active_profile_id"] is not None
                    else None
                )
                if mode == "overwrite" or current["default_uid"] is None:
                    target.execute(
                        "UPDATE users SET default_region_id = ?, default_uid = ?, "
                        "active_profile_id = ?, "
                        "updated_at = ? WHERE qq_id = ?",
                        (
                            default_region_id,
                            default_uid,
                            active_profile_id,
                            _iso(),
                            row["qq_id"],
                        ),
                    )

            source_tables = {
                str(row["name"])
                for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if "player_snapshots" in source_tables:
                source_columns = [
                    str(row["name"])
                    for row in source.execute("PRAGMA table_info(player_snapshots)")
                ]
                target_columns = {
                    str(row["name"])
                    for row in target.execute("PRAGMA table_info(player_snapshots)")
                }
                snapshot_columns = [column for column in source_columns if column in target_columns]
                insert_columns = ", ".join(snapshot_columns)
                placeholders = ", ".join("?" for _ in snapshot_columns)
                updates = ", ".join(
                    f"{column} = excluded.{column}"
                    for column in snapshot_columns
                    if column not in {"region_id", "uid"}
                )
                for row in source.execute(
                    "SELECT player_snapshots.*, game_accounts.qq_id AS source_qq_id "
                    "FROM player_snapshots "
                    "JOIN game_accounts "
                    "ON game_accounts.region_id = player_snapshots.region_id "
                    "AND game_accounts.uid = player_snapshots.uid"
                ):
                    destination = target.execute(
                        "SELECT qq_id FROM game_accounts WHERE region_id = ? AND uid = ?",
                        (row["region_id"], row["uid"]),
                    ).fetchone()
                    if destination is None or str(destination["qq_id"]) != str(row["source_qq_id"]):
                        counts["skipped"] += 1
                        continue
                    values = [row[column] for column in snapshot_columns]
                    if mode == "overwrite":
                        target.execute(
                            f"INSERT INTO player_snapshots ({insert_columns}) "
                            f"VALUES ({placeholders}) "
                            f"ON CONFLICT(region_id, uid) DO UPDATE SET {updates}",
                            values,
                        )
                        counts["snapshots"] += 1
                    else:
                        cursor = target.execute(
                            f"INSERT OR IGNORE INTO player_snapshots ({insert_columns}) "
                            f"VALUES ({placeholders})",
                            values,
                        )
                        counts["snapshots"] += max(0, cursor.rowcount)

            columns = [str(row["name"]) for row in source.execute("PRAGMA table_info(characters)")]
            insert_columns = ", ".join(columns)
            placeholders = ", ".join("?" for _ in columns)
            updates = ", ".join(
                f"{column} = excluded.{column}"
                for column in columns
                if column not in {"profile_id", "character_id"}
            )
            for row in source.execute("SELECT * FROM characters"):
                destination_profile = profile_map.get(int(row["profile_id"]))
                if destination_profile is None:
                    counts["skipped"] += 1
                    continue
                values = [
                    destination_profile if name == "profile_id" else row[name] for name in columns
                ]
                if mode == "overwrite":
                    target.execute(
                        f"INSERT INTO characters ({insert_columns}) VALUES ({placeholders}) "
                        f"ON CONFLICT(profile_id, character_id) DO UPDATE SET {updates}",
                        values,
                    )
                    counts["characters"] += 1
                else:
                    cursor = target.execute(
                        f"INSERT OR IGNORE INTO characters ({insert_columns}) "
                        f"VALUES ({placeholders})",
                        values,
                    )
                    counts["characters"] += max(0, cursor.rowcount)
            return counts
        finally:
            source.close()


def _validate_zip_entry(info: zipfile.ZipInfo) -> None:
    path = PurePosixPath(info.filename)
    if (
        not info.filename
        or path.is_absolute()
        or ".." in path.parts
        or info.file_size > _MAX_ENTRY_BYTES
        or (info.external_attr >> 16) & 0o170000 == 0o120000
    ):
        raise BackupError("备份包含不安全或过大的文件")


def _json_from_zip(source: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        value = json.loads(source.read(name).decode("utf-8-sig"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"备份 JSON 无效：{name}") from exc
    if not isinstance(value, dict):
        raise BackupError(f"备份 JSON 根节点必须是对象：{name}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso() -> str:
    return datetime.now(UTC).isoformat()
