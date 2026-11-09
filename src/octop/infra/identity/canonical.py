"""Read current canonical identities without minting or copying identities.

The retained Moss authority owns token verification, login and revocation. Its
verifier is injected here; HTTP fields and decoded JWT claims are not a verifier.
SQLite reads use the original canonical schema in a separate, read-only store,
never Mosswork's integer ``users`` table. PostgreSQL is not implemented here.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from octop.infra.agents.personal_moss_access import (
    MossAccessDecision,
    MossMemberSnapshot,
    decide_moss_access,
    require_personal_moss_access,
)
from octop.infra.agents.personal_moss_identity import PersonalMossIdentity
from octop.infra.errors import ErrorCode, MossworkError

_HUMAN_CREDENTIALS = {
    "sudowork": {"phone-login", "password-login", "oauth2-login", "cas-login"},
    "moss": {"moss-password-login"},
}


@dataclass(frozen=True)
class VerifiedHumanPrincipal:
    """Output of a trusted authority verifier, never a request-body model.

    The verifier must check signature, issuer, audience, access type, expiry,
    revocation, current member role/status, human account ownership and auth_app.
    API keys, refresh tokens and internal/control credentials are not human
    logins. ``credential_id`` is the verified jti, not a durable login or native
    execution session. The original authority has no persistent login SID;
    ``session_id`` stays absent unless a real authority session proves it.
    """

    authority_id: str
    account_id: str
    organization_id: str
    member_id: str
    credential_role: str
    auth_app: str
    key_id: str
    credential_id: str
    expires_at: int
    session_id: str | None = None


class CanonicalAuthorityVerifier(Protocol):
    """Verify an opaque credential using the existing authority, failing closed."""

    async def verify_human(self, access_token: str) -> VerifiedHumanPrincipal | None: ...


@dataclass(frozen=True)
class CanonicalIdentitySnapshot:
    authority_id: str
    account_id: str
    organization_id: str
    organization_name: str
    member: MossMemberSnapshot
    membership_revision: int
    moss_access: MossAccessDecision
    personal_agent: PersonalMossIdentity | None
    grant_updated_at: int | None
    auth_app: str
    key_id: str
    credential_id: str
    credential_expires_at: int
    session_id: str | None


def _unauthorized() -> MossworkError:
    return MossworkError.localized(ErrorCode.AUTH_FAILED)


def _unavailable(dependency: str) -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": dependency}
    )


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid canonical text field")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Invalid canonical integer field")
    return value


class SqliteCanonicalSnapshotStore:
    """Read only the retained SQLite schema; never initialize or migrate a DB.

    ``database_path`` must be a local Path, not a PostgreSQL URL or a Mosswork
    service pool. A missing file/schema is an unavailable dependency, not proof
    that the user has no membership. Every snapshot uses one read transaction.
    """

    def __init__(self, database_path: Path, *, authority_id: str) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A local SQLite Path is required; PostgreSQL is not supported")
        self.authority_id = _text(authority_id)
        self._uri = database_path.absolute().as_uri() + "?mode=ro"

    def _read(self, principal: VerifiedHumanPrincipal) -> CanonicalIdentitySnapshot:
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                row = connection.execute(
                    """SELECT a.id AS account_id, a.status AS account_status,
                        u.id AS member_id, u.org_id, u.role, u.status AS member_status,
                        u.membership_revision, o.name AS organization_name,
                        g.user_id AS grant_member_id, g.enabled, g.updated_at,
                        EXISTS (SELECT 1 FROM revoked_tokens WHERE jti = ?) AS is_revoked
                    FROM human_account_memberships m
                    JOIN human_accounts a ON a.id = m.account_id
                    JOIN users u ON u.id = m.user_id AND u.org_id = m.org_id
                    JOIN organizations o ON o.id = m.org_id
                    LEFT JOIN moss_member_access g ON g.org_id = m.org_id AND g.user_id = m.user_id
                    WHERE m.account_id = ? AND m.org_id = ? AND m.user_id = ?""",
                    (
                        principal.credential_id,
                        principal.account_id,
                        principal.organization_id,
                        principal.member_id,
                    ),
                ).fetchone()
                if row is None or row["is_revoked"] or row["account_status"] != "active":
                    raise _unauthorized()
                if row["member_status"] != "active" or row["role"] != principal.credential_role:
                    raise _unauthorized()
                member = MossMemberSnapshot(
                    organization_id=_text(row["org_id"]),
                    owner_id=_text(row["member_id"]),
                    role=_text(row["role"]),
                    status=_text(row["member_status"]),
                )
                explicit_grant: bool | None = None
                grant_updated_at: int | None = None
                if row["grant_member_id"] is not None:
                    enabled = _integer(row["enabled"])
                    if enabled not in (0, 1):
                        raise ValueError("Invalid canonical grant")
                    explicit_grant = enabled == 1
                    grant_updated_at = _integer(row["updated_at"])
                decision = decide_moss_access(member, explicit_grant)
                agent = (
                    require_personal_moss_access(
                        member,
                        organization_id=principal.organization_id,
                        owner_id=principal.member_id,
                        explicit_grant=explicit_grant,
                    )
                    if decision.is_enabled
                    else None
                )
                return CanonicalIdentitySnapshot(
                    authority_id=self.authority_id,
                    account_id=_text(row["account_id"]),
                    organization_id=member.organization_id,
                    organization_name=_text(row["organization_name"]),
                    member=member,
                    membership_revision=_integer(row["membership_revision"]),
                    moss_access=decision,
                    personal_agent=agent,
                    grant_updated_at=grant_updated_at,
                    auth_app=principal.auth_app,
                    key_id=principal.key_id,
                    credential_id=principal.credential_id,
                    credential_expires_at=principal.expires_at,
                    session_id=principal.session_id,
                )
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise _unavailable("canonical_identity_store") from exc


class CanonicalSnapshotService:
    """Resolve only the authenticated member; no admin target-owner bypass exists."""

    def __init__(
        self,
        store: SqliteCanonicalSnapshotStore,
        verifier: CanonicalAuthorityVerifier,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._verifier = verifier
        self._clock = clock

    async def snapshot(self, access_token: str) -> CanonicalIdentitySnapshot:
        """Verify with the original authority, then read fresh canonical facts off-loop."""
        if not isinstance(access_token, str) or not access_token.strip():
            raise _unauthorized()
        try:
            principal = await self._verifier.verify_human(access_token)
        except MossworkError:
            raise
        except Exception as exc:
            raise _unavailable("canonical_authority") from exc
        if not isinstance(principal, VerifiedHumanPrincipal):
            raise _unauthorized()
        self._validate_principal(principal)
        snapshot = await asyncio.get_running_loop().run_in_executor(
            None, self._store._read, principal
        )
        self._validate_principal(principal)
        return snapshot

    def _validate_principal(self, principal: VerifiedHumanPrincipal) -> None:
        fields = (
            principal.authority_id,
            principal.account_id,
            principal.organization_id,
            principal.member_id,
            principal.credential_role,
            principal.auth_app,
            principal.key_id,
            principal.credential_id,
        )
        if (
            any(not isinstance(value, str) or not value.strip() for value in fields)
            or principal.authority_id != self._store.authority_id
            or principal.key_id not in _HUMAN_CREDENTIALS.get(principal.auth_app, set())
            or type(principal.expires_at) is not int
            or principal.expires_at <= self._clock()
            or principal.session_id is not None
            and (not isinstance(principal.session_id, str) or not principal.session_id.strip())
        ):
            raise _unauthorized()
