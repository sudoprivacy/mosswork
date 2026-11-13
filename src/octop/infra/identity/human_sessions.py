"""Maintained phone-login lifecycle for the original canonical identity store.

This domain must be composed only after exclusive canonical-writer takeover.
It uses existing accounts/memberships, policy, signing domain and revocations;
it does not initialize schemas, repair identity links, register accounts or
create a local integer JWT, Agent, runtime or model credential.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import (
    CanonicalIdentitySnapshot,
    CanonicalSnapshotService,
    SqliteCanonicalSnapshotStore,
    VerifiedHumanPrincipal,
)
from octop.infra.identity.canonical_jwt import sign_claims
from octop.infra.identity.human_tokens import CanonicalHumanTokenVerifier
from octop.infra.identity.login_policy import SqliteLoginPolicyReader, default_scopes_for_role
from octop.infra.identity.phone_auth import (
    CanonicalPhoneAuthService,
    PhoneCodeSent,
    normalize_phone,
)
from octop.infra.identity.write_authority import CanonicalWriterAdmission, assert_canonical_writer

if TYPE_CHECKING:
    from octop.infra.identity.api_key_exchange import CanonicalApiKeyProof, CanonicalMachineTokens
    from octop.infra.identity.password_auth import CanonicalPasswordProof


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": "canonical_human_sessions"}
    )


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise _unavailable()
    return value


@dataclass(frozen=True, repr=False)
class CanonicalSessionTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    account_id: str
    organization_id: str
    member_id: str
    token_type: str = "Bearer"


@dataclass(frozen=True)
class _Member:
    account_id: str
    organization_id: str
    member_id: str
    role: str


@dataclass(frozen=True)
class _CredentialProof:
    account_id: str
    organization_id: str
    member_id: str
    credential_id: str
    expires_at: int
    role: str | None


class CanonicalHumanSessionService:
    """Authenticate normal phone logins and retain each selected member UUID.

    Writer admission, phone delivery/hash configuration, policy defaults and
    signing configuration all come from trusted composition. Admission is a
    consumer contract, not proof that the old writer has stopped. The provider
    must hold actual exclusion across all admitted writes and awaited effects.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        authority_id: str,
        secret: str,
        issuer: str,
        phone: CanonicalPhoneAuthService | None,
        policy: SqliteLoginPolicyReader,
        writer: CanonicalWriterAdmission,
        access_ttl_seconds: int,
        phone_enabled: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A trusted local canonical SQLite Path is required")
        if type(access_ttl_seconds) is not int or access_ttl_seconds <= 0:
            raise ValueError("A positive controlled access-token TTL is required")
        self._uri = database_path.absolute().as_uri() + "?mode=rw"
        self._secret = _text(secret).encode("utf-8")
        self._issuer = _text(issuer)
        if phone_enabled is not None and not callable(phone_enabled):
            raise TypeError("The trusted effective phone setting reader must be callable")
        self._phone_enabled = phone_enabled
        self._phone, self._policy, self._writer = phone, policy, writer
        self._ttl, self._clock = access_ttl_seconds, clock
        self._verifier = CanonicalHumanTokenVerifier(
            database_path,
            authority_id=authority_id,
            secret=secret,
            issuer=issuer,
            expected_audience=None,
            clock=clock,
        )
        self.snapshots = CanonicalSnapshotService(
            SqliteCanonicalSnapshotStore(database_path, authority_id=authority_id),
            self._verifier,
            clock=clock,
        )

    async def issue_verified_api_key(self, proof: CanonicalApiKeyProof) -> CanonicalMachineTokens:
        """Use this authority's controlled signing domain for an opaque machine proof.

        No human member/account/session is manufactured. The API-key domain
        rechecks the actual key, owner and revocation in this signing transaction.
        """
        from octop.infra.identity.api_key_exchange import CanonicalApiKeyProof

        if type(proof) is not CanonicalApiKeyProof:
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        proof.assert_issuer(self, self._uri)
        await assert_canonical_writer(self._writer)

        def read_and_sign() -> CanonicalMachineTokens:
            try:
                with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as db, db:
                    db.row_factory = sqlite3.Row
                    db.execute("BEGIN IMMEDIATE")
                    return proof.issue_in_transaction(
                        db, self._secret, self._issuer, self._clock, self._ttl
                    )
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        result = await asyncio.get_running_loop().run_in_executor(None, read_and_sign)
        await assert_canonical_writer(self._writer)
        return result

    @property
    def is_phone_login_available(self) -> bool:
        """Read the effective phone setting without changing existing credentials."""
        try:
            enabled = (
                self._phone is not None if self._phone_enabled is None else self._phone_enabled()
            )
        except Exception:
            raise _unavailable() from None
        if type(enabled) is not bool or (enabled and self._phone is None):
            raise _unavailable()
        return enabled

    def _phone_for_login(self) -> CanonicalPhoneAuthService:
        if not self.is_phone_login_available:
            raise MossworkError.localized(ErrorCode.NOT_FOUND, status=404)
        if self._phone is None:
            raise _unavailable()
        return self._phone

    async def send_code(self, raw_phone: object) -> PhoneCodeSent:
        self._phone_for_login()
        await assert_canonical_writer(self._writer)
        return await self._phone_for_login().send_code(raw_phone)

    async def login(self, raw_phone: object, code: object) -> CanonicalSessionTokens:
        self._phone_for_login()
        await assert_canonical_writer(self._writer)
        delivery = self._phone_for_login()
        phone = normalize_phone(raw_phone)
        if phone is None or not await delivery.verify_code(phone, code):
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        member = await asyncio.get_running_loop().run_in_executor(None, self._phone_member, phone)
        return await self._issue_current(member, record_login=True, verified_phone=phone)

    async def switch_organization(
        self, access_token: str, organization_id: str
    ) -> CanonicalSessionTokens:
        snapshot = await self.snapshots.snapshot(access_token)
        self._require_phone(snapshot)
        if not isinstance(organization_id, str) or not organization_id.strip():
            raise MossworkError.localized(ErrorCode.FORBIDDEN)
        member = await asyncio.get_running_loop().run_in_executor(
            None, self._target_member, snapshot.account_id, organization_id.strip()
        )
        # Recheck the original credential after target membership IO. An org
        # password and a super-admin resource override do not cross accounts.
        current = await self.snapshots.snapshot(access_token)
        if current.account_id != snapshot.account_id or current.member != snapshot.member:
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        return await self._issue_current(
            member,
            record_login=False,
            proof=_CredentialProof(
                snapshot.account_id,
                snapshot.organization_id,
                snapshot.member.owner_id,
                snapshot.credential_id,
                snapshot.credential_expires_at,
                snapshot.member.role,
            ),
        )

    async def refresh(self, refresh_token: str) -> CanonicalSessionTokens:
        principal = await self._verifier.verify_refresh_human(refresh_token)
        if principal is None:
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        if (principal.auth_app, principal.key_id) in {
            ("sudowork", "password-login"),
            ("moss", "moss-password-login"),
        }:
            return await self._refresh_password(principal)
        if principal.auth_app != "sudowork" or principal.key_id != "phone-login":
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        member = await asyncio.get_running_loop().run_in_executor(
            None, self._target_member, principal.account_id, principal.organization_id
        )
        if member.member_id != principal.member_id:
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        # Original phone refresh reissues current-role scopes after policy;
        # it is an authority operation, not role-based expansion by a consumer.
        return await self._issue_current(
            member,
            record_login=False,
            proof=_CredentialProof(
                principal.account_id,
                principal.organization_id,
                principal.member_id,
                principal.credential_id,
                principal.expires_at,
                None,
            ),
        )

    async def _refresh_password(self, principal: VerifiedHumanPrincipal) -> CanonicalSessionTokens:
        """Retain the original password realm/scopes while reading its current role."""
        await assert_canonical_writer(self._writer)

        def read_and_sign() -> CanonicalSessionTokens:
            try:
                with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as db, db:
                    db.row_factory = sqlite3.Row
                    db.execute("BEGIN IMMEDIATE")
                    member = self._read_target(
                        db, principal.account_id, principal.organization_id, require_phone=False
                    )
                    if (
                        member.member_id != principal.member_id
                        or principal.expires_at <= self._clock()
                        or db.execute(
                            "SELECT 1 FROM revoked_tokens WHERE jti = ?",
                            (principal.credential_id,),
                        ).fetchone()
                        is not None
                    ):
                        raise MossworkError.localized(ErrorCode.AUTH_FAILED)
                    if principal.key_id == "password-login":
                        self._policy.require_method_in_transaction(
                            db, member.organization_id, "password"
                        )
                    return self._token_pair(
                        member,
                        key_id=principal.key_id,
                        auth_app="moss"
                        if principal.key_id == "moss-password-login"
                        else "sudowork",
                        scopes=principal.scopes,
                    )
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        result = await asyncio.get_running_loop().run_in_executor(None, read_and_sign)
        await assert_canonical_writer(self._writer)
        return result

    async def issue_verified_password(
        self, proof: CanonicalPasswordProof
    ) -> CanonicalSessionTokens:
        """Issue through the same canonical signer after real password proof/current policy."""
        from octop.infra.identity.password_auth import CanonicalPasswordProof

        if type(proof) is not CanonicalPasswordProof:
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        proof.assert_issuer(self, self._uri)
        await assert_canonical_writer(self._writer)

        def read_record_and_sign() -> CanonicalSessionTokens:
            try:
                with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as db, db:
                    db.row_factory = sqlite3.Row
                    db.execute("BEGIN IMMEDIATE")
                    verified = proof.assert_current_in_transaction(db)
                    member = _Member(
                        verified.account_id,
                        verified.organization_id,
                        verified.member_id,
                        verified.role,
                    )
                    if verified.auth_app == "sudowork":
                        self._policy.require_method_in_transaction(
                            db, member.organization_id, "password"
                        )
                    timestamp = math.floor(self._clock() * 1000)
                    if verified.replacement_hash is not None:
                        db.execute(
                            "UPDATE users SET password_hash = ?, password_updated_at = ? "
                            "WHERE id = ? AND org_id = ?",
                            (
                                verified.replacement_hash,
                                timestamp,
                                member.member_id,
                                member.organization_id,
                            ),
                        )
                    db.execute(
                        "UPDATE users SET last_login_at = ? WHERE id = ? AND org_id = ?",
                        (timestamp, member.member_id, member.organization_id),
                    )
                    return self._token_pair(
                        member, key_id=verified.key_id, auth_app=verified.auth_app
                    )
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        result = await asyncio.get_running_loop().run_in_executor(None, read_record_and_sign)
        await assert_canonical_writer(self._writer)
        return result

    async def logout(self, access_token: str, refresh_token: str | None = None) -> None:
        # Signature/type/issuer checks are independent of a now-disabled account
        # so its own valid credentials can still be revoked. No raw claim from
        # a request is trusted. Refresh credentials cannot enter the access path.
        access = self._verifier._verify_claims(access_token, expected_type="access")
        refresh = (
            self._verifier._verify_claims(refresh_token, expected_type="refresh")
            if refresh_token
            else None
        )
        await assert_canonical_writer(self._writer)

        def revoke() -> None:
            try:
                with (
                    closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as db,
                    db,
                ):
                    db.execute("BEGIN IMMEDIATE")
                    for claims in (access, refresh):
                        if claims is not None:
                            db.execute(
                                "INSERT INTO revoked_tokens (jti, expires_at) VALUES (?, ?) "
                                "ON CONFLICT(jti) DO UPDATE SET expires_at = excluded.expires_at",
                                (claims.credential_id, claims.expires_at),
                            )
                    if access is not None:
                        db.execute(
                            "DELETE FROM oauth_provider_tokens WHERE user_id = ?",
                            (access.member_id,),
                        )
                    now = math.floor(self._clock())
                    for table in (
                        "revoked_tokens",
                        "oauth_provider_tokens",
                        "minted_service_tokens",
                    ):
                        db.execute(f"DELETE FROM {table} WHERE expires_at < ?", (now,))
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        await asyncio.get_running_loop().run_in_executor(None, revoke)
        await assert_canonical_writer(self._writer)

    @staticmethod
    def _require_phone(snapshot: CanonicalIdentitySnapshot) -> None:
        if snapshot.auth_app != "sudowork" or snapshot.key_id != "phone-login":
            raise MossworkError.localized(ErrorCode.FORBIDDEN)

    def _phone_member(self, phone: str) -> _Member:
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                account = db.execute(
                    "SELECT id, status FROM human_accounts WHERE phone = ?", (phone,)
                ).fetchone()
                if account is None:
                    # Identity-link backfill and first-use registration require
                    # their own accepted migration/registration path.
                    raise MossworkError.localized(
                        ErrorCode.AUTH_FAILED, status=404, details={"registration_required": True}
                    )
                if account["status"] != "active":
                    raise MossworkError.localized(ErrorCode.FORBIDDEN)
                members = db.execute(
                    """SELECT m.account_id, m.org_id, u.id, u.role, u.phone
                    FROM human_account_memberships m
                    JOIN users u ON u.id = m.user_id AND u.org_id = m.org_id
                    WHERE m.account_id = ? AND u.status = 'active'
                    ORDER BY m.created_at, m.org_id""",
                    (account["id"],),
                ).fetchall()
                if not members:
                    raise MossworkError.localized(ErrorCode.FORBIDDEN)
                selected = next((row for row in members if row["phone"] == phone), members[0])
                return self._member(selected)
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    @staticmethod
    def _member(row: sqlite3.Row) -> _Member:
        return _Member(
            _text(row["account_id"]), _text(row["org_id"]), _text(row["id"]), _text(row["role"])
        )

    def _target_member(self, account_id: str, organization_id: str) -> _Member:
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                return self._read_target(db, account_id, organization_id)
        except (sqlite3.Error, OSError):
            raise _unavailable() from None

    def _read_target(
        self,
        db: sqlite3.Connection,
        account_id: str,
        organization_id: str,
        *,
        require_phone: bool = True,
    ) -> _Member:
        row = db.execute(
            """SELECT m.account_id, m.org_id, u.id, u.role
            FROM human_account_memberships m
            JOIN human_accounts a ON a.id = m.account_id
            JOIN users u ON u.id = m.user_id AND u.org_id = m.org_id
            JOIN organizations o ON o.id = m.org_id
            WHERE a.id = ? AND m.org_id = ? AND a.status = 'active' AND u.status = 'active'"""
            + (" AND a.phone IS NOT NULL AND a.phone <> ''" if require_phone else ""),
            (account_id, organization_id),
        ).fetchone()
        if row is None:
            raise MossworkError.localized(ErrorCode.FORBIDDEN)
        return self._member(row)

    async def _issue_current(
        self,
        expected: _Member,
        *,
        record_login: bool,
        verified_phone: str | None = None,
        proof: _CredentialProof | None = None,
    ) -> CanonicalSessionTokens:
        await assert_canonical_writer(self._writer)
        await self._policy.require_sms(expected.organization_id)
        await assert_canonical_writer(self._writer)

        def read_record_and_sign() -> CanonicalSessionTokens:
            try:
                with (
                    closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as db,
                    db,
                ):
                    db.row_factory = sqlite3.Row
                    db.execute("BEGIN IMMEDIATE")
                    self._policy.require_sms_in_transaction(db, expected.organization_id)
                    if proof is not None:
                        actor = self._read_target(db, proof.account_id, proof.organization_id)
                        if (
                            proof.account_id != expected.account_id
                            or actor.member_id != proof.member_id
                            or (proof.role is not None and actor.role != proof.role)
                            or proof.expires_at <= self._clock()
                            or db.execute(
                                "SELECT 1 FROM revoked_tokens WHERE jti = ?",
                                (proof.credential_id,),
                            ).fetchone()
                            is not None
                        ):
                            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
                    if verified_phone is not None:
                        phone_row = db.execute(
                            "SELECT phone FROM human_accounts WHERE id = ?",
                            (expected.account_id,),
                        ).fetchone()
                        if phone_row is None or phone_row["phone"] != verified_phone:
                            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
                    current = self._read_target(db, expected.account_id, expected.organization_id)
                    if current.member_id != expected.member_id:
                        raise MossworkError.localized(ErrorCode.AUTH_FAILED)
                    if record_login:
                        db.execute(
                            "UPDATE users SET last_login_at = ? WHERE id = ? AND org_id = ?",
                            (
                                math.floor(self._clock() * 1000),
                                current.member_id,
                                current.organization_id,
                            ),
                        )
                    # The credential proof and current subject are checked in
                    # the same write transaction that owns the signing point.
                    return self._token_pair(current)
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        result = await asyncio.get_running_loop().run_in_executor(None, read_record_and_sign)
        await assert_canonical_writer(self._writer)
        return result

    def _token_pair(
        self,
        member: _Member,
        *,
        key_id: str = "phone-login",
        auth_app: Literal["sudowork", "moss"] = "sudowork",
        scopes: tuple[str, ...] | None = None,
    ) -> CanonicalSessionTokens:
        issued_at = math.floor(self._clock())
        return CanonicalSessionTokens(
            self._sign(
                member,
                "access",
                issued_at,
                self._ttl,
                key_id=key_id,
                auth_app=auth_app,
                scopes=scopes,
            ),
            self._sign(
                member,
                "refresh",
                issued_at,
                7 * 24 * 60 * 60,
                key_id=key_id,
                auth_app=auth_app,
                scopes=scopes,
            ),
            self._ttl,
            member.account_id,
            member.organization_id,
            member.member_id,
        )

    def _sign(
        self,
        member: _Member,
        kind: str,
        issued_at: int,
        ttl: int,
        *,
        key_id: str = "phone-login",
        auth_app: Literal["sudowork", "moss"] = "sudowork",
        scopes: tuple[str, ...] | None = None,
    ) -> str:
        if (auth_app, key_id) not in {
            ("sudowork", "phone-login"),
            ("sudowork", "password-login"),
            ("moss", "moss-password-login"),
        } or (
            scopes is not None
            and (type(scopes) is not tuple or any(not isinstance(scope, str) for scope in scopes))
        ):
            raise _unavailable()
        claims: dict[str, object] = {
            "iss": self._issuer,
            "sub": member.member_id,
            "org_id": member.organization_id,
            "role": member.role,
            "scopes": list(default_scopes_for_role(member.role) if scopes is None else scopes),
            "key_id": key_id,
            "auth_app": auth_app,
            **({"account_id": member.account_id} if key_id == "phone-login" else {}),
            "type": kind,
            "jti": str(uuid4()),
            "iat": issued_at,
            "exp": issued_at + ttl,
        }

        return sign_claims(claims, self._secret)
