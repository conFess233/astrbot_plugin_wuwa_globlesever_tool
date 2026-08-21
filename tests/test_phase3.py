import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlparse

from clients.sdk_crypto import encode_password, generate_signature
from domain.login import AuthenticatedAccount, GuidePlayer, SdkLoginResult
from infrastructure.crypto import TokenCipher
from infrastructure.database import Database
from repositories.accounts import AccountError, AccountRepository
from services.login_sessions import LoginConflictError, LoginSessionError, LoginSessionService
from services.settings import PluginSettings


class FakeAuthClient:
    def __init__(self) -> None:
        self.players = (
            GuidePlayer("900001", "漂泊者一号", "os_asia", "亚洲服", 80),
            GuidePlayer("900002", "漂泊者二号", "os_usa", "美洲服", 60),
        )

    @staticmethod
    def new_device_id() -> str:
        return "TEST-DEVICE-ID"

    async def email_login(
        self,
        email: str,
        password: str,
        device_id: str,
        geetest: dict[str, str] | None = None,
    ) -> SdkLoginResult:
        if password != "correct-password":
            raise AssertionError("测试未预期错误密码")
        return SdkLoginResult(False, "sdk-code", "cuid", "account", "auto-token")

    async def complete_login(
        self,
        result: SdkLoginResult,
        device_id: str,
        language: str = "zh-Hans",
    ) -> AuthenticatedAccount:
        return AuthenticatedAccount(
            "cuid",
            "account",
            result.auto_token,
            "access-token",
            "oauth-code",
            "guide-token",
            device_id,
            self.players,
        )


class SdkCryptoTests(unittest.TestCase):
    def test_password_encoding_and_signature_are_deterministic(self) -> None:
        self.assertEqual(encode_password("Passw0rd!"), "FzUGcwc3Qhcm")
        fields = {"b": "2", "a": "1", "sign": "ignored", "geetestLotNumber": "ignored"}
        self.assertEqual(generate_signature(fields), "97d62926de2f57beb886c20827bd69f2")

    def test_geetest_fields_do_not_change_signature(self) -> None:
        plain = generate_signature({"a": "1"})
        challenged = generate_signature({"a": "1", "geetestCaptchaOutput": "secret"})
        self.assertEqual(plain, challenged)


class LoginSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.database = Database(self.path)
        await self.database.initialize()
        self.cipher = TokenCipher(b"p" * 32)
        self.settings = PluginSettings.from_mapping(
            {"public_https_base_url": "https://bot.example.test"}
        )
        self.auth = FakeAuthClient()
        self.service = LoginSessionService(
            self.database,
            self.cipher,
            self.auth,
            self.settings,
        )
        self.cards = Path(self.temp.name) / "cards"
        self.cards.mkdir()
        self.accounts = AccountRepository(self.database, self.cipher, 5, self.cards)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp.cleanup()

    @staticmethod
    def _token(url: str) -> str:
        return unquote(urlparse(url).path.rsplit("/", 1)[-1])

    async def _prepare(
        self,
        qq_id: str,
        email: str,
        selected: list[str],
        default_uid: str,
        *,
        origin_context: str | None = None,
    ):
        origin_context = origin_context or f"aiocqhttp:FriendMessage:{qq_id}"
        link = await self.service.create_link(qq_id, origin_context)
        token = self._token(link.url)
        session = await self.service.exchange_link(token)
        result = await self.service.submit_credentials(
            session.session_token,
            session.csrf_token,
            email,
            "correct-password",
            self.settings.public_https_base_url,
            "203.0.113.10",
        )
        self.assertEqual([player.uid for player in result.players], ["900001", "900002"])
        selection = await self.service.select_uids(
            session.session_token,
            session.csrf_token,
            self.settings.public_https_base_url,
            selected,
            default_uid,
        )
        return selection, origin_context

    async def test_link_is_one_time_and_login_confirmation_binds_multiple_uids(self) -> None:
        selection, origin = await self._prepare(
            "10001",
            "player@example.com",
            ["900001", "900002"],
            "900002",
        )
        with self.assertRaises(LoginSessionError):
            await self.service.confirm_login("10001", origin, "000000")
        attempts = await self.database.read(
            lambda db: int(
                db.execute(
                    "SELECT failed_attempts FROM pending_logins WHERE requesting_qq_id = '10001'"
                ).fetchone()[0]
            )
        )
        self.assertEqual(attempts, 1)

        confirmed = await self.service.confirm_login("10001", origin, selection.confirmation_code)
        self.assertEqual(confirmed.default_uid, "900002")
        overview = await self.accounts.overview("10001")
        self.assertEqual(len(overview.accounts), 2)
        self.assertTrue(next(item for item in overview.accounts if item.uid == "900002").is_active)
        self.assertNotIn(b"correct-password", self.path.read_bytes())

    async def test_login_link_exchange_is_one_time(self) -> None:
        link = await self.service.create_link("10001", "aiocqhttp:FriendMessage:10001")
        token = self._token(link.url)
        await self.service.exchange_link(token)
        with self.assertRaisesRegex(LoginSessionError, "已过期"):
            await self.service.exchange_link(token)

    async def test_relogin_keeps_unselected_existing_uid_and_conflict_is_private(self) -> None:
        first, origin = await self._prepare(
            "10001",
            "player@example.com",
            ["900001", "900002"],
            "900001",
        )
        await self.service.confirm_login("10001", origin, first.confirmation_code)
        second, origin = await self._prepare(
            "10001", "player@example.com", ["900001"], "900001", origin_context=origin
        )
        await self.service.confirm_login("10001", origin, second.confirmation_code)
        self.assertEqual(len((await self.accounts.overview("10001")).accounts), 2)

        conflict, other_origin = await self._prepare(
            "10002", "player@example.com", ["900001"], "900001"
        )
        with self.assertRaisesRegex(LoginConflictError, "已绑定") as raised:
            await self.service.confirm_login("10002", other_origin, conflict.confirmation_code)
        self.assertNotIn("10001", str(raised.exception))

    async def test_switch_and_confirmed_unbind(self) -> None:
        selection, origin = await self._prepare(
            "10001",
            "player@example.com",
            ["900001", "900002"],
            "900001",
        )
        await self.service.confirm_login("10001", origin, selection.confirmation_code)
        self.assertEqual(await self.accounts.switch("10001", "900002"), "900002")
        profile_id = await self.database.read(
            lambda db: int(
                db.execute("SELECT profile_id FROM profiles WHERE uid = '900002'").fetchone()[0]
            )
        )
        card = self.cards / f"profile-{profile_id}-progress-test.png"
        card.write_bytes(b"card")
        pending = await self.accounts.begin_unbind("10001", "900002")
        self.assertIsNone(await self.accounts.confirm_unbind("10001", "000000"))
        self.assertEqual(
            await self.accounts.confirm_unbind("10001", pending.code),
            "UID 900002 已解绑",
        )
        self.assertFalse(card.exists())
        overview = await self.accounts.overview("10001")
        self.assertEqual([item.uid for item in overview.accounts], ["900001"])
        self.assertTrue(overview.accounts[0].is_active)

    async def test_default_uid_requires_reselection_before_unbind(self) -> None:
        selection, origin = await self._prepare(
            "10001",
            "player@example.com",
            ["900001", "900002"],
            "900001",
        )
        await self.service.confirm_login("10001", origin, selection.confirmation_code)
        with self.assertRaisesRegex(AccountError, "默认 UID"):
            await self.accounts.begin_unbind("10001", "900001")


if __name__ == "__main__":
    unittest.main()
