"""Original machine-key exchange/refresh, using the existing canonical issuer.

Pinned contract: Moss d0b84d4c, AuthService.issueTokenFromApiKey/issueToken/
refreshToken/verifyIssuedAccessToken. No human principal, account claim, login
session or resource/native grant is created. This maintained consumer narrows
machine provenance to a retained api_keys row matching its original owner tuple;
unlike the original generic verifier, unknown/deleted key rows are rejected.
Revoked rows still authenticate existing JWTs until expiry/JTI revocation.
"""

from __future__ import annotations

import asyncio
import math
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical_api_keys import CanonicalApiKeyFacts, CanonicalApiKeyService
from octop.infra.identity.canonical_jwt import sign_claims, verified_payload
from octop.infra.identity.human_sessions import CanonicalHumanSessionService
from octop.infra.identity.phone_auth import _JS_WHITESPACE

_SEAL = object()
_HUMAN_KEYS = {"phone-login", "password-login", "moss-password-login", "oauth2-login", "cas-login"}


def _denied() -> MossworkError:
    return MossworkError.localized(ErrorCode.AUTH_FAILED)


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": "canonical_api_key_exchange"}
    )


@dataclass(frozen=True)
class CanonicalMachineCredential:
    """Verified machine capability ceiling; every effect needs its own authorization.

    This DTO is deliberately not a human principal or a native host credential.
    A request-constructed instance is not authentication proof.
    """

    organization_id: str
    member_id: str
    role: str
    scopes: tuple[str, ...]
    key_id: str
    credential_id: str
    expires_at: float


@dataclass(frozen=True, repr=False)
class CanonicalMachineTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    organization_id: str
    member_id: str
    key_id: str
    scopes: tuple[str, ...]
    token_type: str = "Bearer"


def _read_owner(
    db: sqlite3.Connection,
    credential: CanonicalMachineCredential,
    *,
    refresh: bool,
    now: float,
) -> CanonicalApiKeyFacts:
    row = db.execute(
        """SELECT u.role, u.status, m.account_id, m.org_id AS linked_org_id,
        a.status AS account_status FROM users u
        JOIN organizations o ON o.id = u.org_id
        JOIN api_keys k ON k.user_id = u.id AND k.org_id = u.org_id AND k.id = ?
        LEFT JOIN human_account_memberships m ON m.user_id = u.id
        LEFT JOIN human_accounts a ON a.id = m.account_id
        WHERE u.id = ? AND u.org_id = ?""",
        (credential.key_id, credential.member_id, credential.organization_id),
    ).fetchone()
    if (
        row is None
        or row["status"] != "active"
        or not isinstance(row["role"], str)
        or not row["role"]
        or (not refresh and row["role"] != credential.role)
        or credential.expires_at <= math.floor(now)
        or (
            row["account_id"] is not None
            and (
                row["linked_org_id"] != credential.organization_id
                or row["account_status"] != "active"
            )
        )
        or db.execute(
            "SELECT 1 FROM revoked_tokens WHERE jti = ?", (credential.credential_id,)
        ).fetchone()
        is not None
    ):
        raise _denied()
    # Original refresh retains signed scopes, reads the current role, and does
    # not inspect raw key status or adopt changed key scopes after issuance.
    return CanonicalApiKeyFacts(
        credential.key_id,
        credential.organization_id,
        credential.member_id,
        row["role"],
        credential.scopes,
    )


class CanonicalApiKeyProof:
    """Opaque proof sealed to the actual existing issuer and canonical DB URI."""

    __slots__ = ("__issuer", "__uri", "__keys", "__facts", "__secret", "__credential")
    __issuer: CanonicalHumanSessionService
    __uri: str
    __keys: CanonicalApiKeyService
    __facts: CanonicalApiKeyFacts | None
    __secret: str | None
    __credential: CanonicalMachineCredential | None

    def __init__(self) -> None:
        raise TypeError("Machine proofs are created only by the trusted exchange service")

    @classmethod
    def _create(
        cls,
        seal: object,
        issuer: CanonicalHumanSessionService,
        keys: CanonicalApiKeyService,
        facts: CanonicalApiKeyFacts | None,
        secret: str | None,
        credential: CanonicalMachineCredential | None,
    ) -> CanonicalApiKeyProof:
        if seal is not _SEAL:
            raise _denied()
        proof = object.__new__(cls)
        proof.__issuer, proof.__uri, proof.__keys = issuer, issuer._uri, keys
        proof.__facts, proof.__secret, proof.__credential = facts, secret, credential
        return proof

    def __repr__(self) -> str:
        return "CanonicalApiKeyProof(<redacted>)"

    def assert_issuer(self, expected_issuer: object, expected_database_uri: str) -> None:
        if self.__issuer is not expected_issuer or self.__uri != expected_database_uri:
            raise _denied()

    def issue_in_transaction(
        self,
        db: sqlite3.Connection,
        secret: bytes,
        issuer: str,
        clock: Callable[[], float],
        access_ttl: int,
    ) -> CanonicalMachineTokens:
        actual_uri = db.execute("PRAGMA database_list").fetchone()[2]
        if Path(actual_uri).absolute().as_uri() + "?mode=rw" != self.__uri:
            raise _denied()
        if self.__credential is not None:
            facts = _read_owner(db, self.__credential, refresh=True, now=clock())
        else:
            if self.__facts is None or self.__secret is None:
                raise _denied()
            facts = self.__keys._verify_in_transaction(db, self.__facts.key_id, self.__secret)
            if facts != self.__facts:
                raise _denied()
        if not facts.key_id or facts.key_id in _HUMAN_KEYS:
            raise _denied()
        now = math.floor(clock())

        def token(kind: str, ttl: int) -> str:
            return sign_claims(
                {
                    "iss": issuer,
                    "sub": facts.member_id,
                    "org_id": facts.organization_id,
                    "role": facts.current_role,
                    "scopes": list(facts.scopes),
                    "key_id": facts.key_id,
                    "type": kind,
                    "jti": str(uuid4()),
                    "iat": now,
                    "exp": now + ttl,
                },
                secret,
            )

        return CanonicalMachineTokens(
            token("access", access_ttl),
            token("refresh", 7 * 24 * 60 * 60),
            access_ttl,
            facts.organization_id,
            facts.member_id,
            facts.key_id,
            facts.scopes,
        )


class CanonicalApiKeyExchangeService:
    """Exchange only original raw machine keys using one existing issuer.

    No secret/issuer/TTL configuration is accepted here. Both injected services
    must share the exact snapshot authority, canonical DB and writer admission.
    A revoked key prevents new exchange, but does not revoke its signed JWTs.
    Resource/native/derived operations are outside this service's authority.
    """

    def __init__(
        self, keys: CanonicalApiKeyService, *, issuer: CanonicalHumanSessionService
    ) -> None:
        if (
            type(keys) is not CanonicalApiKeyService
            or type(issuer) is not CanonicalHumanSessionService
            or keys._write_uri != issuer._uri
            or keys._snapshots is not issuer.snapshots
            or keys._writer is not issuer._writer
        ):
            raise ValueError("Machine exchange must share the actual canonical issuer and writer")
        self._keys, self._issuer = keys, issuer

    async def exchange(self, raw_key: str) -> CanonicalMachineTokens:
        facts = await self._keys.verify_existing_key(raw_key)
        match = re.fullmatch(
            r"moss_sk_([^.]+)\.([^\n\r\u2028\u2029]+)", raw_key.strip(_JS_WHITESPACE)
        )
        if match is None:
            raise _denied()
        proof = CanonicalApiKeyProof._create(_SEAL, self._issuer, self._keys, facts, match[2], None)
        return await self._issuer.issue_verified_api_key(proof)

    async def refresh(self, refresh_token: str) -> CanonicalMachineTokens:
        credential = self._claims(refresh_token, "refresh")
        if credential is None:
            raise _denied()
        proof = CanonicalApiKeyProof._create(
            _SEAL, self._issuer, self._keys, None, None, credential
        )
        return await self._issuer.issue_verified_api_key(proof)

    def _claims(
        self, token: str, kind: Literal["access", "refresh"]
    ) -> CanonicalMachineCredential | None:
        # The authority's controlled config is reused, never read from a token.
        payload = verified_payload(token, self._issuer._secret)
        if payload is None:
            return None
        strings = ("sub", "org_id", "role", "key_id", "jti")
        exp = payload.get("exp")
        if (
            payload.get("iss") != self._issuer._issuer
            or payload.get("type") != kind
            or any(not isinstance(payload.get(key), str) or not payload[key] for key in strings)
            or any(key in payload for key in ("auth_app", "account_id", "aud", "assistant_id"))
            or payload.get("key_id") in _HUMAN_KEYS
            or not isinstance(payload.get("scopes"), list)
            or isinstance(exp, bool)
            or not isinstance(exp, (int, float))
            or not math.isfinite(exp)
            or exp <= math.floor(self._issuer._clock())
        ):
            return None
        return CanonicalMachineCredential(
            payload["org_id"],
            payload["sub"],
            payload["role"],
            tuple(scope for scope in payload["scopes"] if isinstance(scope, str)),
            payload["key_id"],
            payload["jti"],
            exp,
        )

    async def verify_access(self, access_token: str) -> CanonicalMachineCredential | None:
        """Verify only machine access; current role/JTI/owner checks are read-only."""
        credential = self._claims(access_token, "access")
        if credential is None:
            return None

        def read() -> bool:
            try:
                with closing(sqlite3.connect(self._keys._read_uri, uri=True, timeout=1.0)) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA query_only = ON")
                    db.execute("BEGIN")
                    _read_owner(db, credential, refresh=False, now=self._issuer._clock())
                    return True
            except MossworkError as error:
                if error.code == ErrorCode.AUTH_FAILED:
                    return False
                raise
            except (sqlite3.Error, OSError):
                raise _unavailable() from None

        current = await asyncio.get_running_loop().run_in_executor(None, read)
        return (
            credential
            if current and credential.expires_at > math.floor(self._issuer._clock())
            else None
        )
