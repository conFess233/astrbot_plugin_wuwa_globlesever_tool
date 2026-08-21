import json
import tempfile
import unittest
import warnings
import zipfile

from infrastructure.crypto import TokenCipher
from infrastructure.database import Database
from infrastructure.paths import RuntimePaths
from services.backups import BackupError, BackupService
from services.catalog import CharacterCatalog
from services.dashboard import DashboardError, DashboardService
from services.settings import PluginSettings


class Phase6ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = RuntimePaths.from_plugin_data_root(self.temporary.name)
        self.paths.ensure()
        self.database = Database(self.paths.database)
        await self.database.initialize()
        self.cipher = TokenCipher(b"d" * 32)
        self.config: dict[str, object] = {}
        self.settings = PluginSettings.from_mapping(self.config)
        self.persisted = 0
        self.applied_settings: PluginSettings | None = None
        self.applied_catalog: CharacterCatalog | None = None
        self.service = DashboardService(
            self.database,
            self.paths,
            self.cipher,
            self.config,
            self.settings,
            self._persist,
            self._apply_settings,
            self._apply_catalog,
            self._fetch_catalog,
        )
        self.backups = BackupService(self.database, self.paths, self.cipher)
        await self._seed()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temporary.cleanup()

    async def _persist(self) -> None:
        self.persisted += 1

    async def _apply_settings(self, value: PluginSettings) -> None:
        self.applied_settings = value

    def _apply_catalog(self, value: CharacterCatalog) -> None:
        self.applied_catalog = value

    @staticmethod
    async def _fetch_catalog() -> object:
        return {
            "data": [
                {
                    "roleGbId": "test-role",
                    "texts": [
                        {"language": "zh-Hans", "name": "测试角色"},
                        {"language": "en", "name": "Test Role"},
                    ],
                    "star": 5,
                }
            ]
        }

    async def _seed(self) -> None:
        encrypted = self.cipher.encrypt_text(json.dumps({"guide_token": "secret"}))

        def operation(db):
            db.execute(
                "INSERT INTO users (qq_id, language, default_uid, created_at, updated_at) "
                "VALUES ('10001', 'zh-CN', '900001', 'now', 'now')"
            )
            credential = db.execute(
                "INSERT INTO credentials (qq_id, account_identity_hmac, email_masked, "
                "encrypted_tokens, encrypted_device_id, token_status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'valid', 'now')",
                (
                    "10001",
                    self.cipher.account_identity_hmac("user@example.test"),
                    "u***@example.test",
                    encrypted,
                    self.cipher.encrypt_text("device"),
                ),
            )
            db.execute(
                "INSERT INTO game_accounts (uid, qq_id, credential_id, region_id, "
                "region_name, player_name, sync_status, last_sync_success_at) "
                "VALUES ('900001', '10001', ?, 'os_asia', '亚洲服', '漂泊者', "
                "'success', 'now')",
                (credential.lastrowid,),
            )
            profile = db.execute(
                "INSERT INTO profiles (qq_id, profile_type, uid, updated_at) "
                "VALUES ('10001', 'uid', '900001', 'now')"
            )
            db.execute(
                "UPDATE users SET active_profile_id = ? WHERE qq_id = '10001'",
                (profile.lastrowid,),
            )
            db.execute(
                "INSERT INTO characters (profile_id, character_id, "
                "character_name_snapshot, record_origin, api_owned, score_status, "
                "updated_at) VALUES (?, '1501', '测试角色', 'api', 1, "
                "'unavailable', 'now')",
                (profile.lastrowid,),
            )

        await self.database.write(operation)

    async def test_overview_accounts_and_exact_confirmation(self) -> None:
        overview = await self.service.overview()
        self.assertEqual((overview["users"], overview["accounts"]), (1, 1))
        accounts = await self.service.accounts(query="900001")
        self.assertEqual(accounts["items"][0]["email_masked"], "u***@example.test")
        with self.assertRaises(DashboardError):
            await self.service.force_unbind("admin", "900001", "wrong")
        result = await self.service.force_unbind("admin", "900001", "900001")
        self.assertEqual(result["qq_id"], "10001")
        self.assertEqual((await self.service.overview())["accounts"], 0)

    async def test_config_is_validated_and_saved_as_one_draft(self) -> None:
        with self.assertRaises(DashboardError):
            await self.service.save_config("admin", {"unknown": True})
        result = await self.service.save_config(
            "admin", {"allow_query_others": True, "character_page_size": 18}
        )
        self.assertTrue(result["allow_query_others"])
        self.assertEqual(result["character_page_size"], 18)
        self.assertEqual(self.persisted, 1)
        self.assertTrue(self.applied_settings.allow_query_others)

    async def test_resource_update_rollback_and_scoped_cleanup(self) -> None:
        updated = await self.service.update_resources("admin")
        self.assertEqual(updated["character_count"], 1)
        self.assertEqual(self.applied_catalog.resolve("测试角色").character_id, "test-role")
        (self.paths.media_cards / "profile-1-progress-deadbeef.png").write_bytes(b"card")
        (self.paths.media_temp / "render.tmp").write_bytes(b"temp")
        (self.paths.cache_character / "keep.png").write_bytes(b"keep")
        cleanup = await self.service.cleanup_cache("admin", "清理缓存")
        self.assertEqual(cleanup["removed"], 2)
        self.assertTrue((self.paths.cache_character / "keep.png").is_file())
        rolled = await self.service.rollback_resources("admin", "回滚角色资源")
        self.assertEqual(rolled["source"], "bundled")

    async def test_sanitized_backup_can_be_inspected_and_restored(self) -> None:
        archive = await self.backups.export(
            self.service.config_snapshot(), include_encrypted_credentials=False
        )
        inspection = await self.backups.inspect(archive)
        self.assertFalse(inspection.includes_encrypted_credentials)
        await self.database.write(lambda db: db.execute("DELETE FROM users WHERE qq_id = '10001'"))
        restored = await self.backups.restore(archive, mode="preserve", restore_credentials=False)
        self.assertEqual(restored["users"], 1)
        credential = await self.database.read(
            lambda db: db.execute(
                "SELECT encrypted_tokens, token_status FROM credentials"
            ).fetchone()
        )
        self.assertEqual(credential["encrypted_tokens"], "")
        self.assertEqual(credential["token_status"], "needs_login")

    async def test_wrong_key_and_tampered_archive_are_rejected(self) -> None:
        archive = await self.backups.export(
            self.service.config_snapshot(), include_encrypted_credentials=True
        )
        other = BackupService(self.database, self.paths, TokenCipher(b"x" * 32))
        self.assertGreater((await other.inspect(archive)).invalid_credentials, 0)
        tampered = archive.with_name("tampered.zip")
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w") as target:
            for info in source.infolist():
                value = b"{}" if info.filename == "config.json" else source.read(info)
                target.writestr(info, value)
        tampered.replace(archive)
        with self.assertRaises(BackupError):
            await self.backups.inspect(archive)

    async def test_backup_rejects_duplicate_and_unlisted_entries(self) -> None:
        archive = await self.backups.export(
            self.service.config_snapshot(), include_encrypted_credentials=False
        )
        duplicate = archive.with_name("duplicate.zip")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(archive) as source, zipfile.ZipFile(duplicate, "w") as target:
                for info in source.infolist():
                    target.writestr(info, source.read(info))
                target.writestr("config.json", b"{}")
        with self.assertRaisesRegex(BackupError, "不能重复"):
            await self.backups.inspect(duplicate)

        extra = archive.with_name("extra.zip")
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(extra, "w") as target:
            for info in source.infolist():
                target.writestr(info, source.read(info))
            target.writestr("notes.txt", b"unexpected")
        with self.assertRaisesRegex(BackupError, "完全一致"):
            await self.backups.inspect(extra)


if __name__ == "__main__":
    unittest.main()
