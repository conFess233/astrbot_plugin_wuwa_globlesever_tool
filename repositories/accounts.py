"""已绑定国际服 UID 的查询、切换与解绑。"""

import asyncio
import hmac
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..infrastructure.card_cache import remove_profile_cards
from ..infrastructure.crypto import TokenCipher
from ..infrastructure.database import Database


class AccountError(ValueError):
    """表示账号操作不满足当前状态。"""


@dataclass(frozen=True, slots=True)
class AccountEntry:
    uid: str
    region_id: str
    player_name: str | None
    region_name: str
    is_default: bool
    is_active: bool
    sync_status: str


@dataclass(frozen=True, slots=True)
class AccountOverview:
    email_masked: tuple[str, ...]
    accounts: tuple[AccountEntry, ...]
    active_is_local: bool


@dataclass(frozen=True, slots=True)
class PendingUnbind:
    uid: str
    region_id: str
    code: str


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class AccountRepository:
    def __init__(
        self,
        database: Database,
        cipher: TokenCipher,
        confirm_ttl_minutes: int,
        card_cache_directory: Path | None = None,
    ):
        self.database = database
        self.cipher = cipher
        self.confirm_ttl_minutes = confirm_ttl_minutes
        self.card_cache_directory = card_cache_directory

    async def overview(self, qq_id: str) -> AccountOverview:
        def operation(db: sqlite3.Connection) -> AccountOverview:
            user = db.execute(
                "SELECT default_region_id, default_uid, active_profile_id "
                "FROM users WHERE qq_id = ?",
                (qq_id,),
            ).fetchone()
            if user is None:
                return AccountOverview((), (), True)
            emails = tuple(
                str(row["email_masked"])
                for row in db.execute(
                    "SELECT email_masked FROM credentials WHERE qq_id = ? ORDER BY credential_id",
                    (qq_id,),
                ).fetchall()
            )
            rows = db.execute(
                "SELECT g.*, p.profile_id FROM game_accounts g "
                "JOIN profiles p ON p.region_id = g.region_id AND p.uid = g.uid "
                "WHERE g.qq_id = ? ORDER BY g.region_id, g.uid",
                (qq_id,),
            ).fetchall()
            accounts = tuple(
                AccountEntry(
                    uid=str(row["uid"]),
                    region_id=str(row["region_id"]),
                    player_name=row["player_name"],
                    region_name=str(row["region_name"]),
                    is_default=(
                        str(user["default_region_id"] or "") == str(row["region_id"])
                        and str(user["default_uid"] or "") == str(row["uid"])
                    ),
                    is_active=user["active_profile_id"] == row["profile_id"],
                    sync_status=str(row["sync_status"]),
                )
                for row in rows
            )
            active_type = db.execute(
                "SELECT profile_type FROM profiles WHERE profile_id = ?",
                (user["active_profile_id"],),
            ).fetchone()
            return AccountOverview(
                emails,
                accounts,
                active_type is None or str(active_type["profile_type"]) == "local",
            )

        return await self.database.read(operation)

    async def switch(self, qq_id: str, value: str) -> str:
        target = value.strip()

        def operation(db: sqlite3.Connection) -> str:
            user = db.execute("SELECT 1 FROM users WHERE qq_id = ?", (qq_id,)).fetchone()
            if user is None:
                raise AccountError("你尚未创建本地档案或绑定 UID")
            if target.casefold() == "本地":
                row = db.execute(
                    "SELECT profile_id FROM profiles WHERE qq_id = ? AND profile_type = 'local'",
                    (qq_id,),
                ).fetchone()
                label = "本地"
            else:
                rows = db.execute(
                    "SELECT profile_id FROM profiles WHERE qq_id = ? AND uid = ? "
                    "ORDER BY region_id",
                    (qq_id, target),
                ).fetchall()
                if len(rows) > 1:
                    raise AccountError("该 UID 绑定了多个区服，请通过 /kh 账号 的编号切换")
                row = rows[0] if rows else None
                label = target
            if row is None:
                raise AccountError("未找到你绑定的该 UID")
            db.execute(
                "UPDATE users SET active_profile_id = ?, updated_at = ? WHERE qq_id = ?",
                (int(row["profile_id"]), _iso(), qq_id),
            )
            return label

        return await self.database.write(operation)

    async def begin_unbind(self, qq_id: str, uid: str) -> PendingUnbind:
        uid = uid.strip()
        if not uid:
            raise AccountError("UID 不能为空")
        action_id = uuid.uuid4().hex
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        digest = self.cipher.account_identity_hmac(f"action:{action_id}:{code}")
        expires_at = _now() + timedelta(minutes=self.confirm_ttl_minutes)

        def operation(db: sqlite3.Connection) -> str:
            accounts = db.execute(
                "SELECT region_id FROM game_accounts WHERE uid = ? AND qq_id = ? "
                "ORDER BY region_id",
                (uid, qq_id),
            ).fetchall()
            if not accounts:
                raise AccountError("未找到你绑定的该 UID")
            if len(accounts) > 1:
                active = db.execute(
                    "SELECT p.region_id FROM users u JOIN profiles p "
                    "ON p.profile_id = u.active_profile_id "
                    "WHERE u.qq_id = ? AND p.profile_type = 'uid' AND p.uid = ?",
                    (qq_id, uid),
                ).fetchone()
                if active is None:
                    raise AccountError("该 UID 绑定了多个区服，请先切换到目标账号后再解绑")
                region_id = str(active["region_id"])
            else:
                region_id = str(accounts[0]["region_id"])
            db.execute(
                "DELETE FROM pending_actions WHERE qq_id = ? AND action_type = 'uid_unbind' "
                "AND used_at IS NULL",
                (qq_id,),
            )
            db.execute(
                "INSERT INTO pending_actions (action_id, qq_id, action_type, payload_json, "
                "confirm_code_hash, expires_at, created_at) "
                "VALUES (?, ?, 'uid_unbind', ?, ?, ?, ?)",
                (
                    action_id,
                    qq_id,
                    json.dumps(
                        {"region_id": region_id, "uid": uid},
                        separators=(",", ":"),
                    ),
                    digest,
                    _iso(expires_at),
                    _iso(),
                ),
            )
            return region_id

        region_id = await self.database.write(operation)
        return PendingUnbind(uid, region_id, code)

    async def confirm_unbind(self, qq_id: str, code: str) -> str | None:
        now = _iso()

        def operation(
            db: sqlite3.Connection,
        ) -> tuple[str, tuple[int, ...]] | None:
            rows = db.execute(
                "SELECT * FROM pending_actions WHERE qq_id = ? AND action_type = 'uid_unbind' "
                "AND used_at IS NULL AND expires_at > ?",
                (qq_id, now),
            ).fetchall()
            matched = None
            for row in rows:
                digest = self.cipher.account_identity_hmac(f"action:{row['action_id']}:{code}")
                if hmac.compare_digest(digest, str(row["confirm_code_hash"])):
                    matched = row
                    break
            if matched is None:
                return None
            payload = json.loads(str(matched["payload_json"]))
            uid = str(payload["uid"])
            region_id = str(payload["region_id"])
            account = db.execute(
                "SELECT credential_id FROM game_accounts "
                "WHERE region_id = ? AND uid = ? AND qq_id = ?",
                (region_id, uid, qq_id),
            ).fetchone()
            if account is None:
                db.execute(
                    "UPDATE pending_actions SET used_at = ? WHERE action_id = ?",
                    (now, matched["action_id"]),
                )
                return "该 UID 已经解绑", ()
            profile = db.execute(
                "SELECT profile_id FROM profiles WHERE region_id = ? AND uid = ?",
                (region_id, uid),
            ).fetchone()
            removed_profile_ids = (int(profile["profile_id"]),) if profile else ()
            credential_id = int(account["credential_id"])
            db.execute(
                "DELETE FROM game_accounts WHERE region_id = ? AND uid = ? AND qq_id = ?",
                (region_id, uid, qq_id),
            )
            replacement = db.execute(
                "SELECT region_id, uid FROM game_accounts WHERE qq_id = ? "
                "ORDER BY region_id, uid LIMIT 1",
                (qq_id,),
            ).fetchone()
            if replacement is None:
                local = db.execute(
                    "SELECT profile_id FROM profiles WHERE qq_id = ? AND profile_type = 'local'",
                    (qq_id,),
                ).fetchone()
                db.execute(
                    "UPDATE users SET default_region_id = NULL, default_uid = NULL, "
                    "active_profile_id = ?, updated_at = ? "
                    "WHERE qq_id = ?",
                    (int(local["profile_id"]), now, qq_id),
                )
            else:
                replacement_uid = str(replacement["uid"])
                replacement_region_id = str(replacement["region_id"])
                profile = db.execute(
                    "SELECT profile_id FROM profiles WHERE region_id = ? AND uid = ?",
                    (replacement_region_id, replacement_uid),
                ).fetchone()
                user = db.execute(
                    "SELECT default_region_id, default_uid, active_profile_id "
                    "FROM users WHERE qq_id = ?",
                    (qq_id,),
                ).fetchone()
                deleted_profile_active = (
                    db.execute(
                        "SELECT 1 FROM profiles WHERE profile_id = ?",
                        (user["active_profile_id"],),
                    ).fetchone()
                    is None
                )
                removed_was_default = (
                    str(user["default_region_id"] or "") == region_id
                    and str(user["default_uid"] or "") == uid
                )
                new_default_region = (
                    replacement_region_id if removed_was_default else user["default_region_id"]
                )
                new_default_uid = replacement_uid if removed_was_default else user["default_uid"]
                new_active = (
                    int(profile["profile_id"])
                    if deleted_profile_active
                    else user["active_profile_id"]
                )
                db.execute(
                    "UPDATE users SET default_region_id = ?, default_uid = ?, "
                    "active_profile_id = ?, updated_at = ? WHERE qq_id = ?",
                    (new_default_region, new_default_uid, new_active, now, qq_id),
                )
            remains = db.execute(
                "SELECT 1 FROM game_accounts WHERE credential_id = ? LIMIT 1", (credential_id,)
            ).fetchone()
            if remains is None:
                db.execute("DELETE FROM credentials WHERE credential_id = ?", (credential_id,))
            db.execute("DELETE FROM pending_actions WHERE action_id = ?", (matched["action_id"],))
            return f"UID {uid} 已解绑", removed_profile_ids

        result = await self.database.write(operation)
        if result is None:
            return None
        message, profile_ids = result
        await asyncio.to_thread(remove_profile_cards, self.card_cache_directory, profile_ids)
        return message
