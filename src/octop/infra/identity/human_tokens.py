"""Verify retained human access JWTs against the existing canonical authority.

Wire-format rules come from sudoprivacy/moss at
d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d, src/server/auth/token.ts.
The signing secret, issuer and consumer audience must be supplied by trusted
composition. This adapter issues no tokens, initializes no DB and accepts no
caller-provided principal. PostgreSQL and machine credentials are unsupported.
"""

from __future__ import annotations

import asyncio
import binascii
import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import (
    SqliteCanonicalSnapshotStore,
    VerifiedHumanPrincipal,
)
from octop.infra.identity.canonical_jwt import verified_payload

_HUMAN_KEYS = {
    "sudowork": {"phone-login", "password-login", "oauth2-login", "cas-login"},
    "moss": {"moss-password-login"},
}


@dataclass(frozen=True)
class _HumanClaims:
    member_id: str
    organization_id: str
    role: str
    auth_app: str
    key_id: str
    credential_id: str
    expires_at: int
    account_id: str | None
    scopes: tuple[str, ...]


def _unavailable(dependency: str) -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": dependency}
    )


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _text_role(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _unavailable("canonical_identity_store")
    return value


class CanonicalHumanTokenVerifier:
    """Verify human access credentials, then read current canonical membership.

    ``expected_audience=None`` deliberately consumes original browser access
    tokens without an ``aud`` claim. A purpose-bound consumer must supply its
    exact audience; even explicit JSON null is different from an absent claim.
    No secret, issuer or audience is taken from an HTTP request or JWT payload.

    Account lookup is read-only and never performs the legacy ``ensureExisting``
    repair. The existing snapshot store checks current status, role, revocation
    and grants; reusing its reader avoids another entitlement implementation.
    The snapshot API rechecks those facts after this verifier returns.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        authority_id: str,
        secret: str,
        issuer: str,
        expected_audience: str | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            not _text(secret)
            or not _text(issuer)
            or not _text(authority_id)
            or expected_audience is not None
            and not _text(expected_audience)
        ):
            raise _unavailable("canonical_authority_configuration")
        try:
            self._secret = secret.encode("utf-8")
        except UnicodeError as exc:
            raise _unavailable("canonical_authority_configuration") from exc
        self._issuer = issuer
        self._audience = expected_audience
        self._clock = clock
        self._store = SqliteCanonicalSnapshotStore(database_path, authority_id=authority_id)
        self._uri = database_path.absolute().as_uri() + "?mode=ro"

    async def verify_human(self, access_token: str) -> VerifiedHumanPrincipal | None:
        """Return only a verified, current human member; IO failures remain 503s."""
        claims = self._verify_access_claims(access_token)
        if claims is None:
            return None
        principal = await asyncio.get_running_loop().run_in_executor(
            None, self._verify_current_member, claims
        )
        if principal is not None and principal.expires_at <= self._clock():
            return None
        return principal

    async def verify_refresh_human(self, refresh_token: str) -> VerifiedHumanPrincipal | None:
        """Verify a refresh credential only for the maintained login authority.

        This is separate from verify_human: a refresh token never authenticates
        the self API. Original refresh reads the current role before issuing a
        new pair, while access verification requires the signed role to match.
        """
        claims = self._verify_claims(refresh_token, expected_type="refresh")
        if claims is None:
            return None
        principal = await asyncio.get_running_loop().run_in_executor(
            None, self._verify_current_member, claims, True
        )
        if principal is not None and principal.expires_at <= self._clock():
            return None
        return principal

    def _verify_access_claims(self, token: str) -> _HumanClaims | None:
        return self._verify_claims(token, expected_type="access")

    def _verify_claims(
        self, token: str, *, expected_type: Literal["access", "refresh"]
    ) -> _HumanClaims | None:
        payload = verified_payload(token, self._secret)
        if payload is None:
            return None
        try:
            string_fields = ("sub", "org_id", "key_id", "jti", "role")
            if (
                payload.get("type") != expected_type
                or any(not isinstance(payload.get(key), str) for key in string_fields)
                or not isinstance(payload.get("scopes"), list)
            ):
                return None
            expires = payload.get("exp")
            if isinstance(expires, bool) or not isinstance(expires, (int, float)):
                return None
            if not math.isfinite(expires) or expires <= math.floor(self._clock()):
                return None
            key_id, auth_app = payload["key_id"], payload.get("auth_app")
            # These pair checks preserve token.ts:203-207 before narrowing the
            # accepted credential family to actual human logins below.
            if key_id == "password-login" and auth_app != "sudowork":
                return None
            if "auth_app" in payload and auth_app not in ("moss", "sudowork"):
                return None
            if auth_app == "moss" and key_id != "moss-password-login":
                return None
            if key_id == "moss-password-login" and auth_app != "moss":
                return None
            if "account_id" in payload and (
                not isinstance(payload["account_id"], str) or not payload["account_id"]
            ):
                return None
            if payload.get("iss") != self._issuer:
                return None
            if self._audience is None:
                if "aud" in payload:
                    return None
            elif payload.get("aud") != self._audience:
                return None
            if not isinstance(auth_app, str) or key_id not in _HUMAN_KEYS.get(auth_app, set()):
                return None
            if any(not _text(payload[key]) for key in string_fields):
                return None
            account_id = payload.get("account_id")
            if account_id is not None and key_id != "phone-login":
                # Original verifyIssuedAccessToken calls requireHumanAccount
                # for account-bearing tokens, which accepts only phone-login.
                return None
            return _HumanClaims(
                member_id=payload["sub"],
                organization_id=payload["org_id"],
                role=payload["role"],
                auth_app=auth_app,
                key_id=key_id,
                credential_id=payload["jti"],
                # The original uses floor(now), so a fractional numeric exp is
                # valid until the next integer second; do not truncate its TTL.
                expires_at=math.ceil(expires),
                account_id=account_id,
                # Original hasScope uses includes(string); non-string array
                # entries cannot grant a capability. Freeze before any await.
                scopes=tuple(scope for scope in payload["scopes"] if isinstance(scope, str)),
            )
        except (ValueError, TypeError, OverflowError, UnicodeError, binascii.Error):
            return None

    def _verify_current_member(
        self, claims: _HumanClaims, is_refresh: bool = False
    ) -> VerifiedHumanPrincipal | None:
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                row = connection.execute(
                    """SELECT m.account_id, a.phone AS account_phone, u.role AS current_role
                    FROM human_account_memberships m
                    JOIN human_accounts a ON a.id = m.account_id
                    JOIN users u ON u.id = m.user_id AND u.org_id = m.org_id
                    WHERE m.user_id = ? AND m.org_id = ?""",
                    (claims.member_id, claims.organization_id),
                ).fetchone()
                if row is None:
                    return None
                account_id = row["account_id"]
                if not _text(account_id):
                    raise _unavailable("canonical_identity_store")
                if claims.account_id is not None and claims.account_id != account_id:
                    return None
                if claims.key_id == "phone-login":
                    if not _text(row["account_phone"]):
                        return None
                    if claims.account_id is None:
                        member = connection.execute(
                            "SELECT phone FROM users WHERE id = ? AND org_id = ?",
                            (claims.member_id, claims.organization_id),
                        ).fetchone()
                        if not member or member["phone"] != row["account_phone"]:
                            return None
                principal = VerifiedHumanPrincipal(
                    authority_id=self._store.authority_id,
                    account_id=account_id,
                    organization_id=claims.organization_id,
                    member_id=claims.member_id,
                    credential_role=_text_role(row["current_role"]) if is_refresh else claims.role,
                    auth_app=claims.auth_app,
                    key_id=claims.key_id,
                    credential_id=claims.credential_id,
                    expires_at=claims.expires_at,
                    scopes=claims.scopes,
                )
            # The frozen store intentionally exposes no public authorization
            # entry taking a principal. This trusted adapter reuses its private
            # reader after signature/consumer checks and real account lookup.
            self._store._read(principal)
            return principal
        except MossworkError as exc:
            if exc.code == ErrorCode.AUTH_FAILED:
                return None
            raise
        except (sqlite3.Error, OSError) as exc:
            raise _unavailable("canonical_identity_store") from exc
