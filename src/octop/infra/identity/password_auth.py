"""Retained canonical password verification, using the one maintained issuer.

Ported from moss@d0b84d4c auth/service.ts:1159-1206 and authCenter/db.ts:2151-2188.
This module never signs tokens or creates accounts. The dedicated Moss realm
retains its original policy exemption; ordinary Sudowork passwords require the
current effective organization password policy, including at the signing point.
Legacy bcrypt uses the actual compatible verifier; dependency failure refuses.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, final

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.login_policy import SqliteLoginPolicyReader
from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer

if TYPE_CHECKING:
    from octop.infra.identity.human_sessions import CanonicalSessionTokens

_PROOF_SEAL = object()
_JS_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
type PasswordRealm = Literal["moss", "sudowork"]


def _failed(status: int = 401) -> MossworkError:
    return MossworkError.localized(ErrorCode.AUTH_FAILED, status=status)


def _unavailable(dependency: str = "canonical_password_auth") -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": dependency}
    )


def _utf8(value: str) -> bytes:
    # Node replaces unpaired UTF-16 surrogates with U+FFFD before UTF-8 hashing.
    return (
        value.encode("utf-16-le", errors="surrogatepass")
        .decode("utf-16-le", errors="replace")
        .encode("utf-8")
    )


def _scrypt_record(password: str, salt: str | None = None) -> str:
    salt = secrets.token_hex(16) if salt is None else salt
    try:
        derived = hashlib.scrypt(_utf8(password), salt=_utf8(salt), n=16384, r=8, p=1, dklen=64)
    except (ValueError, MemoryError, OSError):
        raise _unavailable("canonical_password_scrypt") from None
    return "scrypt$" + salt + "$" + derived.hex()


class LegacyBcryptVerifier(Protocol):
    def verify(self, password: str, password_hash: str) -> bool:
        """Verify the original bcryptjs $2a/$2b/$2y and UTF-8 truncation semantics.

        Trusted composition must supply a qualified implementation. This port
        runs in the executor; no live process/network adapter is supplied here.
        """
        ...


class BcryptLegacyVerifier:
    """Use bcrypt 5 with the original bcryptjs byte encoding and 72-byte ceiling.

    No pre-hash or new algorithm is introduced. NUL remains part of the bytes.
    Successful old-password logins upgrade to the original scrypt record format
    in the maintained issuer's canonical transaction.
    """

    def verify(self, password: str, password_hash: str) -> bool:
        try:
            import bcrypt
        except ImportError:
            raise _unavailable("canonical_password_bcrypt") from None
        # bcryptjs combines valid UTF-16 pairs but encodes unpaired surrogates
        # directly, unlike Node crypto's U+FFFD replacement for scrypt.
        encoded = (
            password.encode("utf-16-le", errors="surrogatepass")
            .decode("utf-16-le", errors="surrogatepass")
            .encode("utf-8", errors="surrogatepass")
        )
        try:
            return bcrypt.checkpw(encoded[:72], password_hash.encode("ascii"))
        except (ValueError, UnicodeError):
            return False


def _verify_password(
    password: str, password_hash: object, legacy: LegacyBcryptVerifier | None
) -> tuple[bool, str | None]:
    if not isinstance(password_hash, str) or not password_hash:
        return False, None
    if re.match(r"^\$2[aby]\$[0-9]{2}\$", password_hash):
        if legacy is None:
            raise _unavailable("canonical_password_bcrypt")
        try:
            result = legacy.verify(password, password_hash)
        except Exception:
            raise _unavailable("canonical_password_bcrypt") from None
        if type(result) is not bool:
            raise _unavailable("canonical_password_bcrypt")
        return (True, _scrypt_record(password)) if result else (False, None)
    match = re.fullmatch(r"scrypt\$([^$]+)\$([0-9a-f]+)", password_hash)
    if match is None:
        return False, None
    actual = bytes.fromhex(_scrypt_record(password, match[1]).rsplit("$", 1)[1])
    # Buffer.from(hex) ignores a final unmatched nibble; preserve that detail.
    expected = bytes.fromhex(match[2][: len(match[2]) // 2 * 2])
    return len(actual) == len(expected) and hmac.compare_digest(actual, expected), None


@dataclass(frozen=True)
class VerifiedPasswordMember:
    account_id: str
    organization_id: str
    member_id: str
    role: str
    auth_app: PasswordRealm
    key_id: Literal["password-login", "moss-password-login"]
    replacement_hash: str | None = field(default=None, repr=False)


@dataclass(frozen=True, repr=False)
class _PasswordFacts:
    account_id: str
    organization_id: str
    member_id: str
    role: str
    revision: int
    password_hash: str
    password_updated_at: int | None


def _facts(db: sqlite3.Connection, member_id: str) -> _PasswordFacts:
    row = db.execute(
        """SELECT m.account_id,u.org_id,u.id,u.role,u.membership_revision,
            u.password_hash,u.password_updated_at
        FROM users u JOIN human_account_memberships m ON m.user_id=u.id AND m.org_id=u.org_id
        JOIN human_accounts a ON a.id=m.account_id JOIN organizations o ON o.id=u.org_id
        WHERE u.id=? AND u.status='active' AND a.status='active'""",
        (member_id,),
    ).fetchone()
    if row is None:
        raise _failed()
    values = tuple(row)
    if (
        any(not isinstance(value, str) or not value or "\x00" in value for value in values[:4])
        or type(values[4]) is not int
        or values[4] < 0
        or not isinstance(values[5], str)
        or not values[5]
        or (values[6] is not None and (type(values[6]) is not int or values[6] < 0))
    ):
        raise _failed()
    return _PasswordFacts(*values)


@final
class CanonicalPasswordProof:
    """Opaque server-created proof; no public constructor or JSON principal.

    Its private hash is evidence of this actual password check. The maintained
    issuer must bind itself and its DB, then recheck in BEGIN IMMEDIATE before
    upgrading a hash, recording login and signing. It grants no native effect.
    """

    __slots__ = ("__facts", "__issuer", "__policy", "__realm", "__replacement", "__seal", "__uri")
    __facts: _PasswordFacts
    __issuer: object
    __policy: SqliteLoginPolicyReader
    __realm: PasswordRealm
    __replacement: str | None
    __seal: object
    __uri: str

    def __new__(cls) -> CanonicalPasswordProof:
        raise TypeError("Use CanonicalPasswordAuthService")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("A password proof is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("A password proof is immutable")

    def __repr__(self) -> str:
        return "CanonicalPasswordProof()"

    def _assert_sealed(self) -> None:
        if (
            type(self) is not CanonicalPasswordProof
            or getattr(self, "_CanonicalPasswordProof__seal", None) is not _PROOF_SEAL
        ):
            raise _failed()

    def assert_issuer(self, expected_issuer: object, expected_database_uri: str) -> None:
        self._assert_sealed()
        if self.__issuer is not expected_issuer or self.__uri != expected_database_uri:
            raise _failed()

    def assert_current_in_transaction(self, db: sqlite3.Connection) -> VerifiedPasswordMember:
        """Recheck current hash/account/member/policy in the issuer's transaction.

        No DB write or token signing occurs here. password_updated_at is used
        only to reject drift during this login, not to invalidate issued JWTs.
        The original JWT verifier/refresh has no password timestamp rule.
        """
        self._assert_sealed()
        if not db.in_transaction:
            raise _unavailable()
        try:
            main = next(
                (row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"), None
            )
            if not main or Path(main).absolute().as_uri() + "?mode=rw" != self.__uri:
                raise _failed()
            if _facts(db, self.__facts.member_id) != self.__facts:
                raise _failed()
            if self.__realm == "sudowork":
                self.__policy.require_method_in_transaction(
                    db, self.__facts.organization_id, "password"
                )
            return VerifiedPasswordMember(
                self.__facts.account_id,
                self.__facts.organization_id,
                self.__facts.member_id,
                self.__facts.role,
                self.__realm,
                "moss-password-login" if self.__realm == "moss" else "password-login",
                self.__replacement,
            )
        except (sqlite3.Error, OSError):
            raise _unavailable() from None


class CanonicalPasswordTokenIssuer(Protocol):
    async def issue_verified_password(
        self, proof: CanonicalPasswordProof
    ) -> CanonicalSessionTokens:
        """Consume the sealed proof using the existing canonical signing service."""
        ...


class CanonicalPasswordAuthService:
    """Verify original existing members, without schema/identity initialization.

    Username lookup is global and exact; duplicates are 409. Nonempty username
    takes precedence over email, matching the original service. Account/member
    links must already exist. A password does not authorize switching orgs.
    """

    def __init__(
        self,
        database_path: Path,
        policy: SqliteLoginPolicyReader,
        *,
        writer: CanonicalWriterAdmission,
        issuer: CanonicalPasswordTokenIssuer,
        legacy_bcrypt: LegacyBcryptVerifier | None = None,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A trusted existing canonical SQLite Path is required")
        prefix = database_path.absolute().as_uri()
        if policy._uri != prefix + "?mode=ro":
            raise ValueError("Password policy and verifier must share the canonical DB")
        if not callable(getattr(issuer, "issue_verified_password", None)):
            raise TypeError("The maintained canonical password issuer is required")
        self._read_uri, self._write_uri = prefix + "?mode=ro", prefix + "?mode=rw"
        self._policy, self._writer, self._issuer = policy, writer, issuer
        self._legacy = BcryptLegacyVerifier() if legacy_bcrypt is None else legacy_bcrypt

    async def login_sudowork(
        self, *, password: str, username: str = "", email: str = ""
    ) -> CanonicalSessionTokens:
        return await self._login(username, email, password, "sudowork")

    async def login_moss(
        self, *, password: str, username: str = "", email: str = ""
    ) -> CanonicalSessionTokens:
        return await self._login(username, email, password, "moss")

    def _verify(
        self, username: str, email: str, password: str, realm: PasswordRealm
    ) -> CanonicalPasswordProof:
        try:
            with closing(sqlite3.connect(self._read_uri, uri=True, timeout=1.0)) as db:
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                if username:
                    matches = db.execute(
                        "SELECT id FROM users WHERE name=? ORDER BY created_at ASC", (username,)
                    ).fetchall()
                    if len(matches) > 1:
                        raise _failed(409)
                    selected = matches[0] if matches else None
                else:
                    selected = db.execute(
                        "SELECT id FROM users WHERE email=? LIMIT 1", (email,)
                    ).fetchone()
                if selected is None:
                    raise _failed()
                facts = _facts(db, selected[0])
            verified, replacement = _verify_password(password, facts.password_hash, self._legacy)
            if not verified:
                raise _failed()
            proof = object.__new__(CanonicalPasswordProof)
            for name, value in (
                ("facts", facts),
                ("issuer", self._issuer),
                ("policy", self._policy),
                ("realm", realm),
                ("replacement", replacement),
                ("uri", self._write_uri),
                ("seal", _PROOF_SEAL),
            ):
                object.__setattr__(proof, "_CanonicalPasswordProof__" + name, value)
            return proof
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    async def _login(
        self, username: str, email: str, password: str, realm: PasswordRealm
    ) -> CanonicalSessionTokens:
        if not all(isinstance(value, str) for value in (username, email, password)):
            raise _failed(400)
        username, email = username.strip(_JS_WHITESPACE), email.strip(_JS_WHITESPACE)
        if (not username and not email) or not password:
            raise _failed(400)
        await assert_canonical_writer(self._writer)
        proof = await asyncio.get_running_loop().run_in_executor(
            None, self._verify, username, email, password, realm
        )
        await assert_canonical_writer(self._writer)
        result = await self._issuer.issue_verified_password(proof)
        await assert_canonical_writer(self._writer)
        return result
