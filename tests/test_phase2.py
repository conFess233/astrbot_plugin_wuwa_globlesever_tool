import tempfile
import unittest
from pathlib import Path

from commands.parser import CommandName, CommandParseError, CommandParser
from infrastructure.crypto import TokenCipher
from infrastructure.database import Database
from repositories.local_data import LocalDataError, LocalDataRepository
from services.catalog import CatalogError, CharacterCatalog
from services.command_service import CommandService, CommandServiceError
from services.settings import PluginSettings


class ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = PluginSettings.from_mapping({"extra_command_roots": ["/ww"]})
        self.parser = CommandParser(self.settings)

    def test_formal_keyword_page_and_extra_root(self) -> None:
        cases = (
            ("/kh 角色 2页", CommandName.CHARACTER_LIST, ("2",)),
            ("kh角色2页", CommandName.CHARACTER_LIST, ("2",)),
            ("kh角色 2页", CommandName.CHARACTER_LIST, ("2",)),
            ("/ww 角色 长离", CommandName.CHARACTER_DETAIL, ("长离",)),
            ("鸣潮练度", CommandName.PROGRESS, ()),
            ("/kh 同步", CommandName.SYNC, ()),
            ("/kh 同步 900001", CommandName.SYNC, ("900001",)),
        )
        for text, name, arguments in cases:
            with self.subTest(text=text):
                result = self.parser.parse(text, [])
                self.assertIsNotNone(result)
                self.assertEqual(result.name, name)
                self.assertEqual(result.arguments, arguments)

    def test_target_only_allowed_for_read_commands(self) -> None:
        result = self.parser.parse("kh练度", ["10002"])
        self.assertEqual(result.target_qq, "10002")
        with self.assertRaisesRegex(CommandParseError, "不能指定其他用户"):
            self.parser.parse("/kh 修改 长离 等级 90", ["10002"])
        with self.assertRaisesRegex(CommandParseError, "一次只能"):
            self.parser.parse("kh角色", ["10002", "10003"])

    def test_invalid_page_and_chat_text_do_not_match(self) -> None:
        with self.assertRaisesRegex(CommandParseError, "页码"):
            self.parser.parse("kh角色0页", [])
        self.assertIsNone(self.parser.parse("我觉得kh角色很好看", []))

    def test_modify_role_name_can_contain_spaces(self) -> None:
        result = self.parser.parse("/kh 修改 Rover: Aero 共鸣链 6", [])
        self.assertEqual(result.name, CommandName.MODIFY)
        self.assertEqual(result.arguments, ("Rover: Aero", "共鸣链", "6"))

    def test_runtime_settings_refresh_extra_roots(self) -> None:
        self.parser.update_settings(PluginSettings.from_mapping({"extra_command_roots": ["/new"]}))
        self.assertIsNone(self.parser.parse("/ww 角色", []))
        result = self.parser.parse("/new 角色", [])
        self.assertEqual(result.name, CommandName.CHARACTER_LIST)


class CatalogTests(unittest.TestCase):
    def test_bundled_catalog_resolves_id_and_multilingual_name(self) -> None:
        catalog = CharacterCatalog.load_bundled()
        self.assertGreaterEqual(len(catalog.characters), 50)
        expected_languages = {"zh-CN", "zh-TW", "en", "ja", "ko"}
        self.assertTrue(
            all(set(character.names) == expected_languages for character in catalog.characters)
        )
        self.assertEqual(catalog.resolve("1205").display_name, "长离")
        self.assertEqual(catalog.resolve("changli").character_id, "1205")
        self.assertEqual(catalog.resolve("長離").character_id, "1205")
        self.assertEqual(catalog.resolve("장리").character_id, "1205")
        with self.assertRaises(CatalogError):
            catalog.resolve("不存在的角色")


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "test.sqlite3")
        await self.database.initialize()
        self.catalog = CharacterCatalog.load_bundled()
        self.cards = Path(self.temp.name) / "cards"
        self.cards.mkdir()
        self.repository = LocalDataRepository(self.database, TokenCipher(b"k" * 32), 5, self.cards)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp.cleanup()

    async def test_manual_character_lifecycle_keeps_zero_chain(self) -> None:
        changli = self.catalog.resolve("长离")
        await self.repository.set_manual_field("10001", changli, "等级", 90)
        await self.repository.set_manual_field("10001", changli, "共鸣链", 0)
        profile = await self.repository.active_profile("10001")
        record = await self.repository.get_character(profile.profile_id, changli.character_id)
        self.assertEqual((record.level, record.chain), (90, 0))

        record = await self.repository.reset_manual_fields("10001", changli.character_id, "等级")
        self.assertIsNotNone(record)
        self.assertIsNone(record.level)
        self.assertEqual(record.chain, 0)

        record = await self.repository.reset_manual_fields("10001", changli.character_id, "全部")
        self.assertIsNone(record)
        self.assertEqual(await self.repository.list_characters(profile.profile_id), [])

    async def test_api_value_has_priority_over_manual_value(self) -> None:
        changli = self.catalog.resolve("长离")
        await self.repository.set_manual_field("10001", changli, "等级", 80)
        profile = await self.repository.active_profile("10001")

        def add_api_value(db):
            db.execute(
                "UPDATE characters SET api_level = 90, record_origin = 'mixed' "
                "WHERE profile_id = ? AND character_id = ?",
                (profile.profile_id, changli.character_id),
            )

        await self.database.write(add_api_value)
        record = await self.repository.get_character(profile.profile_id, changli.character_id)
        self.assertEqual(record.level, 90)

    async def test_external_query_does_not_create_target(self) -> None:
        with self.assertRaises(LocalDataError):
            await self.repository.active_profile("99999", external_query=True)
        count = await self.database.read(
            lambda db: int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        )
        self.assertEqual(count, 0)

    async def test_delete_and_clear_confirmation_are_one_time(self) -> None:
        changli = self.catalog.resolve("长离")
        await self.repository.set_manual_field("10001", changli, "等级", 90)
        profile = await self.repository.active_profile("10001")
        detail_card = self.cards / f"profile-{profile.profile_id}-character-test.png"
        detail_card.write_bytes(b"card")
        pending = await self.repository.begin_character_delete("10001", changli.character_id)
        self.assertEqual(await self.repository.confirm("10001", pending.code), "角色记录已删除")
        self.assertFalse(detail_card.exists())
        with self.assertRaises(LocalDataError):
            await self.repository.confirm("10001", pending.code)

        await self.repository.set_manual_field("10001", changli, "共鸣链", 6)
        list_card = self.cards / f"profile-{profile.profile_id}-characters-test.png"
        list_card.write_bytes(b"card")
        pending = await self.repository.begin_clear_data("10001")
        self.assertEqual(
            await self.repository.confirm("10001", pending.code),
            "你的插件数据已全部清除",
        )
        count = await self.database.read(
            lambda db: int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        )
        self.assertEqual(count, 0)
        self.assertFalse(list_card.exists())


class CommandServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "test.sqlite3")
        await self.database.initialize()
        self.settings = PluginSettings.from_mapping({})
        self.catalog = CharacterCatalog.load_bundled()
        self.repository = LocalDataRepository(self.database, TokenCipher(b"z" * 32), 5)
        self.service = CommandService(self.repository, self.catalog, self.settings)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp.cleanup()

    async def test_unowned_character_can_be_created_and_score_is_placeholder(self) -> None:
        command = CommandParser(self.settings).parse("/kh 修改 长离 等级 90", [])
        result = await self.service.execute("10001", command)
        self.assertIn("等级修改为 90", result)
        detail = await self.service.execute(
            "10001", CommandParser(self.settings).parse("/kh 角色 长离", [])
        )
        self.assertIn("等级：90", detail)
        self.assertIn("评分：---", detail)

    async def test_other_user_query_respects_global_switch(self) -> None:
        command = CommandParser(self.settings).parse("kh练度", ["10002"])
        with self.assertRaisesRegex(CommandServiceError, "未开启"):
            await self.service.execute("10001", command)

    async def test_weapon_modification_is_explicitly_unavailable(self) -> None:
        command = CommandParser(self.settings).parse("/kh 修改 长离 武器 赫奕流明", [])
        with self.assertRaisesRegex(CommandServiceError, "武器静态目录"):
            await self.service.execute("10001", command)


if __name__ == "__main__":
    unittest.main()
