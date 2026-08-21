import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from domain.cards import CardCharacter, CardMessage, CharacterDetailCard
from repositories.local_data import CharacterRecord, ProfileSelection
from services.cards import AstrBotCardRenderer, CardService
from services.catalog import CharacterCatalog
from services.resource_cache import StaticImageCache


class CardRendererTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_snapshot_hits_cache_and_changed_snapshot_replaces_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = root / "card.html"
            template.write_text("<p>{{ heading }}</p>", encoding="utf-8")
            generated = root / "generated.png"
            calls: list[dict[str, object]] = []

            async def render(_template, data, _options):
                calls.append(data)
                generated.write_bytes(b"\x89PNG\r\n\x1a\nrendered")
                return str(generated)

            renderer = AstrBotCardRenderer(template, root / "cache", render, 5)
            model = self._detail_model("2026-08-21T01:00:00+00:00")
            first = await renderer.render(model)
            second = await renderer.render(model)
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)

            changed = replace(
                model,
                character=replace(model.character, updated_at="2026-08-21T02:00:00+00:00"),
            )
            third = await renderer.render(changed)
            self.assertNotEqual(first, third)
            self.assertEqual(len(calls), 2)
            self.assertFalse(first.exists())
            self.assertTrue(third.exists())

    @staticmethod
    def _detail_model(updated_at: str) -> CharacterDetailCard:
        return CharacterDetailCard(
            kind="character_detail",
            scope="profile-1-character-1413",
            heading="清宵 · 纯本地档案",
            profile_note="纯本地数据，未经接口验证",
            character=CardCharacter(
                character_id="1413",
                name="清宵",
                image_url=None,
                star=5,
                element_id="4",
                element_image_url=None,
                origin="manual",
                level=90,
                level_source="manual",
                chain=3,
                chain_source="manual",
                weapon_id=None,
                weapon_source=None,
                weapon_level=None,
                weapon_refinement=None,
                score="---",
                updated_at=updated_at,
            ),
        )


class CardServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.catalog = CharacterCatalog.load_bundled()
        self.profile = ProfileSelection(1, "local", None)
        self.record = CharacterRecord(
            profile_id=1,
            character_id="1413",
            character_name="清宵",
            record_origin="mixed",
            level=90,
            chain=2,
            weapon_id="21050036",
            weapon_level=None,
            weapon_refinement=None,
            score_total=None,
            score_grade=None,
            level_source="api",
            chain_source="manual",
            weapon_source="api",
            updated_at="2026-08-21T01:00:00+00:00",
        )

    async def test_no_renderer_returns_complete_detail_text(self):
        result = await CardService(self.catalog).character_detail(
            self.profile, "纯本地", self.record
        )
        self.assertIsInstance(result, str)
        self.assertIn("纯本地数据，未经接口验证", result)
        self.assertIn("等级：90（接口）", result)
        self.assertIn("共鸣链：2（手动）", result)
        self.assertIn("最后更新：2026-08-21 01:00:00 UTC", result)
        self.assertIn("评分：---", result)

    async def test_renderer_failure_degrades_to_same_text(self):
        class FailingRenderer:
            async def render(self, _model):
                raise RuntimeError("browser unavailable")

        with self.assertLogs("services.cards", level="WARNING") as captured:
            result = await CardService(self.catalog, FailingRenderer()).character_detail(
                self.profile, "纯本地", self.record
            )
        self.assertIsInstance(result, str)
        self.assertIn("武器：21050036（接口）", result)
        self.assertNotIn("browser unavailable", result)
        self.assertIn("已降级为文本", captured.output[0])

    async def test_success_returns_image_and_full_fallback(self):
        class SuccessfulRenderer:
            async def render(self, _model):
                return Path("card.png")

        result = await CardService(self.catalog, SuccessfulRenderer()).character_detail(
            self.profile, "纯本地", self.record
        )
        self.assertIsInstance(result, CardMessage)
        self.assertEqual(result.image_path, Path("card.png"))
        self.assertIn("评分：---", result.fallback_text)

    async def test_progress_uses_objective_statistics_only(self):
        result = await CardService(self.catalog).progress(
            self.profile, "纯本地练度概览", [self.record]
        )
        self.assertIn("高等级角色（≥80）：1", result)
        self.assertIn("高共鸣链角色（≥3）：0", result)
        self.assertIn("核心资料完整率：100%", result)
        self.assertIn("评分：---", result)


class StaticImageCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_downloaded_resource_is_reused_as_local_data_uri(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = 0

            async def fetch(_url, _max_bytes):
                nonlocal calls
                calls += 1
                return b"\x89PNG\r\n\x1a\nimage"

            cache = StaticImageCache(Path(temporary), fetch)
            first = await cache.data_uri("https://guide-res.aki-game.net/example.png")
            second = await cache.data_uri("https://guide-res.aki-game.net/example.png")
            self.assertEqual(first, second)
            self.assertTrue(first.startswith("data:image/png;base64,"))
            self.assertEqual(calls, 1)
