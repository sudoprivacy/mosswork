"""Retained canonical member profile and self password changes, without issuing identity.

Moss d0b84d4c auth/service.ts:1875-1898,2946-2966 is the source contract.
These self operations require a current human login, not a new business scope
or organization password-login policy. Machine/refresh tokens remain excluded
by the real injected snapshot authority. This module grants no runtime access.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import math
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import CanonicalIdentitySnapshot, CanonicalSnapshotService
from octop.infra.identity.current_human import assert_current_human
from octop.infra.identity.password_auth import (
    _JS_WHITESPACE,
    BcryptLegacyVerifier,
    LegacyBcryptVerifier,
    _scrypt_record,
    _utf8,
    _verify_password,
)
from octop.infra.identity.profile_avatar import normalize_profile_avatar
from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": "canonical_self_profile"}
    )


def _invalid() -> MossworkError:
    return MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=400)


def _js_length(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


@dataclass(frozen=True, repr=False)
class CanonicalSelfProfile:
    account_id: str
    organization_id: str
    member_id: str
    login_name: str
    nickname: str
    avatar: str | None
    can_change_password: bool


@dataclass(frozen=True, repr=False)
class _PasswordChange:
    original_hash: str
    original_updated_at: int | None
    login_name: str
    replacement_hash: str


def _avatar(value: object) -> str | None:
    """Read only the retained normalized WebP representation; accept no upload.

    The original writer decodes/crops/re-encodes images before persisting this
    value. This bounded data-URL guard is not that decoder or an image rewrite.
    Corrupt/foreign stored values fail closed, never become HTML/remote URLs.
    """
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > math.ceil(2 * 1024 * 1024 / 3) * 4 + 64:
        raise _unavailable()
    match = re.fullmatch(r"data:image/webp;base64,([A-Za-z0-9+/]+={0,2})", value)
    if match is None:
        raise _unavailable()
    try:
        raw = base64.b64decode(match[1], validate=True)
    except binascii.Error:
        raise _unavailable() from None
    if len(raw) > 2 * 1024 * 1024 or raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        raise _unavailable()
    return value


class CanonicalSelfProfileService:
    """Operate on the token's real member, never all members of an account.

    The original profile has no extra signed business-scope gate. Existing
    current human/account/member checks are mandatory, including after awaited
    dependencies and inside each transaction. Writes need the real canonical
    writer; no schema creation/backfill or issuer/configuration is added.
    """

    def __init__(
        self,
        database_path: Path,
        snapshots: CanonicalSnapshotService,
        *,
        writer: CanonicalWriterAdmission,
        legacy_bcrypt: LegacyBcryptVerifier | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A trusted original canonical SQLite Path is required")
        self._read_uri = database_path.absolute().as_uri() + "?mode=ro"
        self._write_uri = database_path.absolute().as_uri() + "?mode=rw"
        if (
            not isinstance(snapshots, CanonicalSnapshotService)
            or snapshots._store._uri != self._read_uri
        ):
            raise ValueError("Profile and snapshot must share the original canonical SQLite store")
        self._snapshots, self._writer, self._clock = snapshots, writer, clock
        self._legacy = BcryptLegacyVerifier() if legacy_bcrypt is None else legacy_bcrypt

    def _now_ms(self) -> int:
        now = self._clock()
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or now < 0
        ):
            raise _unavailable()
        return math.floor(now * 1000)

    async def _same_current(self, token: str, expected: CanonicalIdentitySnapshot) -> None:
        if await self._snapshots.snapshot(token) != expected:
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)

    def _profile_in_transaction(
        self, db: sqlite3.Connection, snapshot: CanonicalIdentitySnapshot
    ) -> CanonicalSelfProfile:
        assert_current_human(db, snapshot, self._clock)
        row = db.execute(
            """SELECT u.name, u.display_name, u.password_hash, a.data_url
            FROM users u LEFT JOIN user_profile_avatars a ON a.user_id=u.id
            WHERE u.id=? AND u.org_id=?""",
            (snapshot.member.owner_id, snapshot.organization_id),
        ).fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise _unavailable()
        return CanonicalSelfProfile(
            snapshot.account_id,
            snapshot.organization_id,
            snapshot.member.owner_id,
            row[0],
            row[1] or row[0],
            _avatar(row[3]),
            bool(row[2]),
        )

    def _read(self, snapshot: CanonicalIdentitySnapshot) -> CanonicalSelfProfile:
        try:
            with closing(sqlite3.connect(self._read_uri, uri=True, timeout=1.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                return self._profile_in_transaction(db, snapshot)
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    async def profile(self, access_token: str) -> CanonicalSelfProfile:
        snapshot = await self._snapshots.snapshot(access_token)
        result = await asyncio.get_running_loop().run_in_executor(None, self._read, snapshot)
        await self._same_current(access_token, snapshot)
        return result

    async def update_avatar(self, access_token: str, data_url: object) -> str | None:
        """Re-encode this current member's avatar, or clear only its avatar row."""
        snapshot = await self._snapshots.snapshot(access_token)
        avatar = await asyncio.get_running_loop().run_in_executor(
            None, normalize_profile_avatar, data_url
        )
        # Decoding can be slow; no previous login or writer decision is reused.
        await self._same_current(access_token, snapshot)
        await assert_canonical_writer(self._writer)

        def write() -> None:
            try:
                with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    assert_current_human(db, snapshot, self._clock)
                    if avatar is None:
                        db.execute(
                            "DELETE FROM user_profile_avatars WHERE user_id=?",
                            (snapshot.member.owner_id,),
                        )
                    else:
                        db.execute(
                            """INSERT INTO user_profile_avatars(user_id,data_url) VALUES (?,?)
                            ON CONFLICT(user_id) DO UPDATE SET data_url=excluded.data_url""",
                            (snapshot.member.owner_id, avatar),
                        )
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        await asyncio.get_running_loop().run_in_executor(None, write)
        await assert_canonical_writer(self._writer)
        await self._same_current(access_token, snapshot)
        return avatar

    async def update_nickname(self, access_token: str, nickname: str) -> None:
        snapshot = await self._snapshots.snapshot(access_token)
        if not isinstance(nickname, str):
            raise _invalid()
        nickname = nickname.strip(_JS_WHITESPACE)
        if not nickname or _js_length(nickname) > 60:
            raise _invalid()
        # Node's original SQLite binding stores UTF-8 replacement for a lone
        # UTF-16 surrogate; validate JS length before converting the DB value.
        nickname = _utf8(nickname).decode("utf-8")
        await assert_canonical_writer(self._writer)

        def write() -> None:
            try:
                with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    assert_current_human(db, snapshot, self._clock)
                    db.execute(
                        "UPDATE users SET display_name=? WHERE id=? AND org_id=?",
                        (nickname, snapshot.member.owner_id, snapshot.organization_id),
                    )
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        await asyncio.get_running_loop().run_in_executor(None, write)
        await assert_canonical_writer(self._writer)
        await self._same_current(access_token, snapshot)

    def _prepare_password(
        self, snapshot: CanonicalIdentitySnapshot, old_password: str, new_password: str
    ) -> _PasswordChange:
        try:
            with closing(sqlite3.connect(self._read_uri, uri=True, timeout=1.0)) as db:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                assert_current_human(db, snapshot, self._clock)
                row = db.execute(
                    "SELECT password_hash,password_updated_at,name FROM users WHERE id=? AND org_id=?",
                    (snapshot.member.owner_id, snapshot.organization_id),
                ).fetchone()
                if row is None:
                    raise MossworkError.localized(ErrorCode.AUTH_FAILED)
            verified, _ = _verify_password(old_password, row[0], self._legacy)
            if not verified:
                raise MossworkError.localized(ErrorCode.FORBIDDEN)
            if (
                not 8 <= _js_length(new_password) <= 20
                or re.search(r"[A-Z]", new_password) is None
                or re.search(r"[a-z]", new_password) is None
                or re.search(r"[0-9]", new_password) is None
            ):
                raise _invalid()
            return _PasswordChange(row[0], row[1], row[2], _scrypt_record(new_password))
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    async def change_password(
        self,
        access_token: str,
        old_password: str,
        new_password: str,
        *,
        moss_only: bool = False,
    ) -> None:
        snapshot = await self._snapshots.snapshot(access_token)
        if moss_only and snapshot.auth_app != "moss":
            raise MossworkError.localized(ErrorCode.FORBIDDEN)
        if not isinstance(old_password, str) or not isinstance(new_password, str):
            raise _invalid()
        prepared = await asyncio.get_running_loop().run_in_executor(
            None, self._prepare_password, snapshot, old_password, new_password
        )
        await self._same_current(access_token, snapshot)
        await assert_canonical_writer(self._writer)

        def write() -> None:
            try:
                with closing(sqlite3.connect(self._write_uri, uri=True, timeout=1.0)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    assert_current_human(db, snapshot, self._clock)
                    row = db.execute(
                        "SELECT password_hash,password_updated_at,name FROM users WHERE id=? AND org_id=?",
                        (snapshot.member.owner_id, snapshot.organization_id),
                    ).fetchone()
                    if row != (
                        prepared.original_hash,
                        prepared.original_updated_at,
                        prepared.login_name,
                    ):
                        raise MossworkError.localized(ErrorCode.AUTH_FAILED)
                    db.execute(
                        "UPDATE users SET password_hash=?,password_updated_at=?,local_auth=1 WHERE id=? AND org_id=?",
                        (
                            prepared.replacement_hash,
                            self._now_ms(),
                            snapshot.member.owner_id,
                            snapshot.organization_id,
                        ),
                    )
                    if (
                        db.execute(
                            "SELECT 1 FROM user_auth_identities WHERE user_id=? AND provider='password' AND issuer='moss' LIMIT 1",
                            (snapshot.member.owner_id,),
                        ).fetchone()
                        is None
                    ):
                        db.execute(
                            """INSERT INTO user_auth_identities
                            (id,org_id,user_id,provider,issuer,normalized_subject,metadata_json,created_at,updated_at)
                            VALUES (?,?,?,'password','moss',?,'{}',?,?)""",
                            (
                                str(uuid4()),
                                snapshot.organization_id,
                                snapshot.member.owner_id,
                                prepared.login_name,
                                self._now_ms(),
                                self._now_ms(),
                            ),
                        )
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        await asyncio.get_running_loop().run_in_executor(None, write)
        await assert_canonical_writer(self._writer)
        await self._same_current(access_token, snapshot)
