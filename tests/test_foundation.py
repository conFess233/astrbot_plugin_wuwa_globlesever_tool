import asyncio
import base64
import os
import sqlite3
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from constants import MASTER_KEY_ENV, PLUGIN_NAME, SCHEMA_VERSION
from infrastructure.crypto import CryptoError, MasterKeyProvider, TokenCipher
from infrastructure.database import _SCHEMA_V1, Database
from infrastructure.paths import RuntimePaths
from services.settings import PluginSettings, SettingsError


class RuntimePathsTests(unittest.TestCase):
    def test_all_paths_are_below_plugin_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = RuntimePaths.from_plugin_data_root(temporary)
            paths.ensure()
            self.assertEqual(paths.root.name, PLUGIN_NAME)
            for field in fields(paths):
                value = Path(getattr(paths, field.name)).resolve()
                self.assertTrue(value.is_relative_to(paths.root.resolve()))


class CryptoTests(unittest.TestCase):
    def test_generated_key_and_cipher_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = MasterKeyProvider(Path(temporary))
            key = provider.load_or_create()
            self.assertEqual(key, provider.load_or_create())
            cipher = TokenCipher(key)
            encrypted = cipher.encrypt_text("token-value")
            self.assertNotIn("token-value", encrypted)
            self.assertEqual(cipher.decrypt_text(encrypted), "token-value")

    def test_environment_key_is_supported(self) -> None:
        key = bytes(range(32))
        encoded = base64.urlsafe_b64encode(key).decode()
        with patch.dict(os.environ, {MASTER_KEY_ENV: encoded}):
            provider = MasterKeyProvider(Path("unused"))
            self.assertEqual(provider.load_or_create(), key)

    def test_invalid_environment_key_is_rejected(self) -> None:
        with (
            patch.dict(os.environ, {MASTER_KEY_ENV: "invalid"}),
            self.assertRaises(CryptoError),
        ):
            MasterKeyProvider(Path("unused")).load_or_create()


class DatabaseTests(unittest.TestCase):
    def test_schema_initialization_is_idempotent(self) -> None:
        async def exercise(path: Path) -> None:
            database = Database(path)
            await database.initialize()
            await database.initialize()
            health = await database.health()
            self.assertEqual(health["schema_version"], SCHEMA_VERSION)
            self.assertEqual(health["integrity"], "ok")
            await database.close()

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(exercise(Path(temporary) / "wuwa.sqlite3"))

    def test_v1_database_is_migrated_without_recreation(self) -> None:
        async def exercise(path: Path) -> None:
            connection = sqlite3.connect(path)
            connection.executescript(_SCHEMA_V1)
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
            connection.close()

            database = Database(path)
            await database.initialize()
            columns = await database.read(
                lambda db: {
                    str(row["name"])
                    for row in db.execute("PRAGMA table_info(pending_logins)").fetchall()
                }
            )
            rate_table = await database.read(
                lambda db: db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'login_rate_limits'"
                ).fetchone()
            )
            self.assertTrue({"status", "session_token_hash", "csrf_token_hash"} <= columns)
            self.assertIsNotNone(rate_table)
            character_columns = await database.read(
                lambda db: {
                    str(row["name"])
                    for row in db.execute("PRAGMA table_info(characters)").fetchall()
                }
            )
            self.assertIn("api_source_order", character_columns)
            await database.close()

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(exercise(Path(temporary) / "v1.sqlite3"))


class SettingsTests(unittest.TestCase):
    def test_defaults_and_root_normalization(self) -> None:
        settings = PluginSettings.from_mapping({"extra_command_roots": ["ww", "/kh"]})
        self.assertEqual(settings.extra_command_roots, ("/ww",))
        self.assertFalse(settings.allow_query_others)

    def test_http_public_url_is_rejected(self) -> None:
        with self.assertRaises(SettingsError):
            PluginSettings.from_mapping({"public_https_base_url": "http://example.com"})

    def test_empty_keywords_are_rejected(self) -> None:
        with self.assertRaises(SettingsError):
            PluginSettings.from_mapping({"keyword_help": []})


if __name__ == "__main__":
    unittest.main()
