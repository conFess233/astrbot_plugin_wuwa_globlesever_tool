import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from domain.sync import (
    GuideAuthenticationError,
    GuideAvatar,
    GuideError,
    GuideIntroduction,
    GuideRoleDetail,
    GuideSyncPlayer,
)
from infrastructure.crypto import TokenCipher
from infrastructure.database import Database
from repositories.local_data import LocalDataRepository
from services.catalog import CharacterCatalog
from services.settings import PluginSettings
from services.sync import GuideSyncService, SyncError


class FakeGuideClient:
    def __init__(self) -> None:
        self.players_calls = 0
        self.fail_detail = False
        self.expire_once = False
        self.missing_fields = False
        self.delay = 0.0

    async def login(self, c_uid: str, c_name: str, access_token: str, language: str) -> str:
        return "refreshed-guide-token"

    async def players(self, token: str, language: str) -> tuple[GuideSyncPlayer, ...]:
        self.players_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.expire_once and token == "guide-token":
            raise GuideAuthenticationError("expired")
        return (GuideSyncPlayer("900001", "os_asia", "亚洲服", "测试玩家"),)

    async def choose_player(self, token: str, language: str, uid: str, region_id: str) -> None:
        return None

    async def avatars(self, token: str, language: str) -> tuple[GuideAvatar, ...]:
        return (GuideAvatar("1205", True, 0), GuideAvatar("1204", False, 1))

    async def introductions(
        self, token: str, language: str, role_id: str
    ) -> tuple[GuideIntroduction, ...]:
        return (GuideIntroduction(100, (language,), 1),)

    async def introduction_detail(
        self, token: str, language: str, role_id: str, introduction_id: int
    ) -> GuideRoleDetail | None:
        if self.fail_detail:
            raise GuideError("invalid detail")
        if self.missing_fields:
            return GuideRoleDetail(None, None, None)
        return GuideRoleDetail(3, True, "weapon-new")


class GuideSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "sync.sqlite3")
        await self.database.initialize()
        self.cipher = TokenCipher(b"s" * 32)
        self.settings = PluginSettings.from_mapping({})
        self.catalog = CharacterCatalog.load_bundled()
        self.client = FakeGuideClient()
        self.service = GuideSyncService(
            self.database, self.cipher, self.client, self.catalog, self.settings
        )
        await self._seed_account()

    async def asyncTearDown(self) -> None:
        await self.service.close()
        await self.database.close()
        self.temp.cleanup()

    async def _seed_account(self) -> None:
        sensitive = self.cipher.encrypt_text(
            json.dumps(
                {
                    "c_uid": "cuid",
                    "c_name": "account",
                    "access_token": "access-token",
                    "guide_token": "guide-token",
                }
            )
        )

        def operation(db):
            now = "2026-08-21T00:00:00+00:00"
            db.execute(
                "INSERT INTO users (qq_id, language, default_uid, created_at, updated_at) "
                "VALUES ('10001', 'zh-CN', '900001', ?, ?)",
                (now, now),
            )
            db.execute(
                "INSERT INTO credentials (qq_id, account_identity_hmac, email_masked, "
                "encrypted_tokens, encrypted_device_id, updated_at) "
                "VALUES ('10001', 'identity', 'p***@example.com', ?, 'encrypted-device', ?)",
                (sensitive, now),
            )
            credential_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.execute(
                "INSERT INTO game_accounts (uid, qq_id, credential_id, region_id, "
                "region_name) VALUES ('900001', '10001', ?, 'os_asia', '亚洲服')",
                (credential_id,),
            )
            db.execute(
                "INSERT INTO profiles (qq_id, profile_type, uid, updated_at) "
                "VALUES ('10001', 'uid', '900001', ?)",
                (now,),
            )
            uid_profile = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.execute(
                "INSERT INTO profiles (qq_id, profile_type, uid, updated_at) "
                "VALUES ('10001', 'local', NULL, ?)",
                (now,),
            )
            local_profile = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.execute(
                "UPDATE users SET active_profile_id = ? WHERE qq_id = '10001'",
                (local_profile,),
            )
            db.execute(
                "INSERT INTO characters (profile_id, character_id, character_name_snapshot, "
                "record_origin, api_owned, api_chain, api_weapon_id, api_weapon_present, "
                "manual_level, manual_weapon_level, manual_weapon_refinement, updated_at) "
                "VALUES (?, '1205', '长离', 'mixed', 1, 1, 'weapon-old', 1, 80, 70, 2, ?)",
                (uid_profile, now),
            )
            db.execute(
                "INSERT INTO characters (profile_id, character_id, character_name_snapshot, "
                "record_origin, manual_chain, updated_at) "
                "VALUES (?, '1204', '莫特斐', 'manual', 4, ?)",
                (uid_profile, now),
            )

        await self.database.write(operation)

    async def test_default_uid_sync_is_atomic_and_preserves_manual_fallbacks(self) -> None:
        result = await self.service.sync("10001")
        self.assertEqual((result.uid, result.owned_count), ("900001", 1))
        repository = LocalDataRepository(self.database, self.cipher, 5)
        profile = await self.database.read(
            lambda db: int(
                db.execute("SELECT profile_id FROM profiles WHERE uid = '900001'").fetchone()[0]
            )
        )
        changli = await repository.get_character(profile, "1205")
        mortefi = await repository.get_character(profile, "1204")
        self.assertEqual((changli.level, changli.chain, changli.weapon_id), (80, 3, "weapon-new"))
        self.assertIsNone(changli.weapon_level)
        self.assertEqual(mortefi.chain, 4)
        status = await self.database.read(
            lambda db: db.execute(
                "SELECT sync_status FROM game_accounts WHERE uid = '900001'"
            ).fetchone()[0]
        )
        self.assertEqual(status, "success")

    async def test_failed_detail_does_not_replace_current_snapshot(self) -> None:
        before = await self.database.read(
            lambda db: tuple(
                db.execute(
                    "SELECT api_chain, api_weapon_id FROM characters WHERE character_id = '1205'"
                ).fetchone()
            )
        )
        self.client.fail_detail = True
        with self.assertRaisesRegex(SyncError, "保留上次同步数据"):
            await self.service.sync("10001")
        after = await self.database.read(
            lambda db: tuple(
                db.execute(
                    "SELECT api_chain, api_weapon_id FROM characters WHERE character_id = '1205'"
                ).fetchone()
            )
        )
        self.assertEqual(before, after)

    async def test_successful_missing_fields_keep_previous_api_values(self) -> None:
        self.client.missing_fields = True
        await self.service.sync("10001")
        values = await self.database.read(
            lambda db: tuple(
                db.execute(
                    "SELECT api_chain, api_weapon_id, manual_weapon_level "
                    "FROM characters WHERE character_id = '1205'"
                ).fetchone()
            )
        )
        self.assertEqual(values, (1, "weapon-old", 70))

    async def test_concurrent_requests_share_one_flight_and_expired_token_refreshes(self) -> None:
        self.client.delay = 0.05
        first, second = await asyncio.gather(
            self.service.sync("10001"), self.service.sync("10001", "900001")
        )
        self.assertEqual(first, second)
        self.assertEqual(self.client.players_calls, 1)

        self.client.players_calls = 0
        self.client.delay = 0
        self.client.expire_once = True
        await self.service.sync("10001")
        encrypted = await self.database.read(
            lambda db: str(db.execute("SELECT encrypted_tokens FROM credentials").fetchone()[0])
        )
        payload = json.loads(self.cipher.decrypt_text(encrypted))
        self.assertEqual(payload["guide_token"], "refreshed-guide-token")


if __name__ == "__main__":
    unittest.main()
