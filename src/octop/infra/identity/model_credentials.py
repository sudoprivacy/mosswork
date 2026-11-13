"""Read actual-owner SudoRouter bindings without reading secret values.

Retained source: sudoprivacy/moss@d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d,
AuthService.getUserModelCredential, OrganizationBillingService.credential and
readSudorouterAccount. Only the complete canonical SQLite schema is supported;
missing organization tables are not evidence that personal billing applies.

This migration consumer is stricter than the original readers in two places:
organization account/token Router IDs must match (the original writer invariant),
and personal references must use the existing provisioning/migration namespace.
Other original-reader inputs are not claimed to be compatible.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from octop.infra.agents.personal_moss_access import (
    MossMemberSnapshot,
    require_personal_moss_access,
)
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import CanonicalIdentitySnapshot, CanonicalSnapshotService

_PERSONAL_NAMESPACE = "moss:sudorouter-users"
_ORGANIZATION_NAMESPACE = "moss:organization-model-keys"


@dataclass(frozen=True)
class NexusModelSecretReference:
    """Exact retained Nexus lookup arguments, with no secret material or client."""

    namespace: str = field(repr=False)
    key: str = field(repr=False)
    subject: str = field(repr=False)

    @property
    def uri(self) -> str:
        """Return the retained reference representation for an explicit consumer."""
        return f"nexus://{self.namespace}/{self.key}"


@dataclass(frozen=True)
class OwnerModelCredentialBinding:
    authority_id: str
    organization_id: str
    member_id: str
    billing_mode: Literal["personal", "organization_shared"]
    external_account_id: str
    external_token_id: int | None
    token_binding_id: str | None = field(repr=False)
    nexus: NexusModelSecretReference = field(repr=False)
    provider: Literal["sudorouter"] = "sudorouter"


@dataclass(frozen=True)
class RegisteredMemberModelCredentials:
    """Current registered member and credential-reference facts, without a grant.

    Ordinary SudoWork Agents do not require Moss use rights. Trusted callers
    must select the owner from an authorized registered resource, separately
    enforce its kind and exact operation/model/native policy, and revalidate
    these facts after waits before resolving a secret. No login is fabricated.
    """

    authority_id: str
    account_id: str
    member: MossMemberSnapshot
    membership_revision: int
    binding: OwnerModelCredentialBinding | None = field(repr=False)


@dataclass(frozen=True)
class RegisteredOwnerModelCredentials:
    """Retained target-owner facts, not a human login or execution permission.

    IM and administrator-selected resources can use their real registered
    owner without fabricating that person's JWT. Trusted composition must
    independently authorize the real caller, exact resource and model action,
    then revalidate these facts after waits and before resolving any secret.
    """

    authority_id: str
    account_id: str
    member: MossMemberSnapshot
    membership_revision: int
    explicit_moss_grant: bool | None
    grant_updated_at: int | None
    binding: OwnerModelCredentialBinding | None = field(repr=False)


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR,
        status=503,
        details={"dependency": "canonical_model_credential_store"},
    )


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid owner credential identity")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("Invalid Router identity")
    return value


def _read_model_binding(
    connection: sqlite3.Connection, *, authority_id: str, organization_id: str, member_id: str
) -> OwnerModelCredentialBinding | None:
    """Read the original shared/personal selection without minting a grant."""
    account = connection.execute(
        "SELECT router_user_id, status FROM organization_model_accounts WHERE org_id = ?",
        (organization_id,),
    ).fetchone()
    if account is not None:
        if account["status"] != "ready":
            return None
        token = connection.execute(
            """SELECT id, router_user_id, router_token_id, secret_ref, status
            FROM organization_model_tokens
            WHERE org_id = ? AND member_id = ? AND purpose = 'member'""",
            (organization_id, member_id),
        ).fetchone()
        if token is None or token["status"] != "ready" or not token["secret_ref"]:
            return None
        router_account_id = _integer(account["router_user_id"])
        # Original ensureToken copies the organization account ID
        # into its member token. A broken binding is not guessed.
        if _integer(token["router_user_id"]) != router_account_id:
            raise ValueError("Router account and member token disagree")
        return OwnerModelCredentialBinding(
            authority_id=authority_id,
            organization_id=organization_id,
            member_id=member_id,
            billing_mode="organization_shared",
            external_account_id=str(router_account_id),
            external_token_id=(
                None if token["router_token_id"] is None else _integer(token["router_token_id"])
            ),
            token_binding_id=_text(token["id"]),
            nexus=NexusModelSecretReference(
                _ORGANIZATION_NAMESPACE,
                _text(token["secret_ref"]),
                f"org:{organization_id}",
            ),
        )
    personal = connection.execute(
        """SELECT external_account_id, token_secret_ref
        FROM billing_external_accounts
        WHERE provider = 'sudorouter' AND owner_type = 'user' AND owner_id = ?""",
        (member_id,),
    ).fetchone()
    if personal is None or not personal["token_secret_ref"]:
        return None
    reference = _text(personal["token_secret_ref"])
    if not reference.startswith("nexus://"):
        raise ValueError("Unsupported owner credential reference")
    namespace, separator, key = reference[len("nexus://") :].rpartition("/")
    # Retained provisioning and both migration writers use this
    # owner namespace. Never substitute a deployment namespace.
    if not separator or namespace != _PERSONAL_NAMESPACE or not key:
        raise ValueError("Unsupported owner credential namespace")
    return OwnerModelCredentialBinding(
        authority_id=authority_id,
        organization_id=organization_id,
        member_id=member_id,
        billing_mode="personal",
        external_account_id=_text(personal["external_account_id"]),
        external_token_id=None,
        token_binding_id=None,
        nexus=NexusModelSecretReference(namespace, key, f"org:{organization_id}"),
    )


def _read_registered_member(
    connection: sqlite3.Connection, *, organization_id: str, member_id: str
) -> tuple[str, MossMemberSnapshot, int]:
    """Read the exact active canonical tuple in the caller's transaction."""
    row = connection.execute(
        """SELECT m.account_id, a.status AS account_status,
            u.id, u.org_id, u.status, u.role, u.membership_revision
        FROM users u
        JOIN organizations o ON o.id = u.org_id
        JOIN human_account_memberships m ON m.user_id = u.id AND m.org_id = u.org_id
        JOIN human_accounts a ON a.id = m.account_id
        WHERE u.org_id = ? AND u.id = ?""",
        (organization_id, member_id),
    ).fetchone()
    if row is None or row["account_status"] != "active" or row["status"] != "active":
        raise MossworkError.localized(ErrorCode.FORBIDDEN, status=403)
    revision = _integer(row["membership_revision"])
    if revision < 0:
        raise ValueError("Invalid membership revision")
    return (
        _text(row["account_id"]),
        MossMemberSnapshot(
            organization_id=_text(row["org_id"]),
            owner_id=_text(row["id"]),
            role=_text(row["role"]),
            status=_text(row["status"]),
        ),
        revision,
    )


class SqliteOwnerModelCredentialStore:
    """Read original bindings in one read-only transaction; never provision.

    An organization account row selects shared billing regardless of status.
    Unavailable shared credentials never fall back to a personal account, a
    service-purpose token, a deployment key or plaintext legacy user columns.
    """

    def __init__(self, database_path: Path, *, authority_id: str) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("A local SQLite Path is required; PostgreSQL is not supported")
        self.authority_id = _text(authority_id)
        self._uri = database_path.absolute().as_uri() + "?mode=ro"

    def _read(self, snapshot: CanonicalIdentitySnapshot) -> OwnerModelCredentialBinding | None:
        if (
            snapshot.authority_id != self.authority_id
            or snapshot.member.organization_id != snapshot.organization_id
        ):
            raise MossworkError.localized(ErrorCode.AUTH_FAILED)
        organization_id = snapshot.organization_id
        member_id = snapshot.member.owner_id
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                member = connection.execute(
                    """SELECT u.status, u.role, a.status AS account_status,
                        g.user_id AS grant_member_id, g.enabled,
                        EXISTS (SELECT 1 FROM revoked_tokens WHERE jti = ?) AS is_revoked
                    FROM human_account_memberships m
                    JOIN users u ON u.id = m.user_id AND u.org_id = m.org_id
                    JOIN human_accounts a ON a.id = m.account_id
                    LEFT JOIN moss_member_access g ON g.org_id = m.org_id AND g.user_id = m.user_id
                    WHERE m.account_id = ? AND m.org_id = ? AND m.user_id = ?""",
                    (snapshot.credential_id, snapshot.account_id, organization_id, member_id),
                ).fetchone()
                if (
                    member is None
                    or member["status"] != "active"
                    or member["account_status"] != "active"
                    or member["role"] != snapshot.member.role
                    or member["is_revoked"]
                ):
                    raise MossworkError.localized(ErrorCode.AUTH_FAILED)
                explicit_grant = None
                if member["grant_member_id"] is not None:
                    if type(member["enabled"]) is not int or member["enabled"] not in (0, 1):
                        raise ValueError("Invalid current Moss entitlement")
                    explicit_grant = bool(member["enabled"])
                require_personal_moss_access(
                    snapshot.member,
                    organization_id=organization_id,
                    owner_id=member_id,
                    explicit_grant=explicit_grant,
                )
                return _read_model_binding(
                    connection,
                    authority_id=self.authority_id,
                    organization_id=organization_id,
                    member_id=member_id,
                )
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise _unavailable() from exc

    def get_for_registered_member(
        self, *, organization_id: str, member_id: str
    ) -> RegisteredMemberModelCredentials:
        """Read member/binding facts without requiring or granting Moss access.

        This synchronous repository method is only a trusted composition
        input, never a browser-selected policy switch or an Agent grant.
        Offload it from async consumers. Missing shared keys stay missing.
        """
        organization_id = _text(organization_id)
        member_id = _text(member_id)
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                account_id, member, revision = _read_registered_member(
                    connection,
                    organization_id=organization_id,
                    member_id=member_id,
                )
                return RegisteredMemberModelCredentials(
                    authority_id=self.authority_id,
                    account_id=account_id,
                    member=member,
                    membership_revision=revision,
                    binding=_read_model_binding(
                        connection,
                        authority_id=self.authority_id,
                        organization_id=organization_id,
                        member_id=member_id,
                    ),
                )
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise _unavailable() from exc

    def assert_registered_member_current(self, expected: RegisteredMemberModelCredentials) -> None:
        """Reject changed member/key facts; Moss entitlement is a separate policy."""
        if (
            type(expected) is not RegisteredMemberModelCredentials
            or expected.authority_id != self.authority_id
        ):
            raise MossworkError.localized(ErrorCode.FORBIDDEN, status=403)
        current = self.get_for_registered_member(
            organization_id=expected.member.organization_id, member_id=expected.member.owner_id
        )
        if current != expected:
            raise MossworkError.localized(ErrorCode.FORBIDDEN, status=403)

    def get_for_registered_owner(
        self, *, organization_id: str, member_id: str
    ) -> RegisteredOwnerModelCredentials:
        """Read a real Moss owner tuple with current Moss use rights.

        This synchronous, read-only repository API is for trusted providers,
        never an HTTP endpoint or an authorization decision. Offload it from
        async consumers. The caller must obtain the exact owner tuple from
        the original authorized resource/IM mapping and enforce its current
        actor/model/native policy separately. A missing key remains absent;
        shared billing never falls back to another owner's credential.
        """
        organization_id = _text(organization_id)
        member_id = _text(member_id)
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                account_id, member, revision = _read_registered_member(
                    connection,
                    organization_id=organization_id,
                    member_id=member_id,
                )
                row = connection.execute(
                    """SELECT enabled, updated_at FROM moss_member_access
                    WHERE org_id = ? AND user_id = ?""",
                    (organization_id, member_id),
                ).fetchone()
                explicit = None
                updated_at = None
                if row is not None:
                    enabled = _integer(row["enabled"])
                    if enabled not in (0, 1):
                        raise ValueError("Invalid current Moss entitlement")
                    explicit = bool(enabled)
                    updated_at = _integer(row["updated_at"])
                    if updated_at < 0:
                        raise ValueError("Invalid current Moss grant revision")
                require_personal_moss_access(
                    member,
                    organization_id=organization_id,
                    owner_id=member_id,
                    explicit_grant=explicit,
                )
                return RegisteredOwnerModelCredentials(
                    authority_id=self.authority_id,
                    account_id=account_id,
                    member=member,
                    membership_revision=revision,
                    explicit_moss_grant=explicit,
                    grant_updated_at=updated_at,
                    binding=_read_model_binding(
                        connection,
                        authority_id=self.authority_id,
                        organization_id=organization_id,
                        member_id=member_id,
                    ),
                )
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise _unavailable() from exc

    def assert_registered_owner_current(self, expected: RegisteredOwnerModelCredentials) -> None:
        """Reject changed target facts or credential references; grant no new effect."""
        if (
            type(expected) is not RegisteredOwnerModelCredentials
            or expected.authority_id != self.authority_id
        ):
            raise MossworkError.localized(ErrorCode.FORBIDDEN, status=403)
        current = self.get_for_registered_owner(
            organization_id=expected.member.organization_id, member_id=expected.member.owner_id
        )
        if current != expected:
            raise MossworkError.localized(ErrorCode.FORBIDDEN, status=403)


class OwnerModelCredentialService:
    """Resolve current human/self ownership and required Moss entitlement only.

    A binding is not execution permission. The trusted consumer must separately
    enforce the signed operation scope, resource policy and native admission,
    and resolve the reference using its real owner-authorized Nexus provider.
    """

    def __init__(
        self, snapshots: CanonicalSnapshotService, store: SqliteOwnerModelCredentialStore
    ) -> None:
        self._snapshots = snapshots
        self._store = store

    async def binding(self, access_token: str) -> OwnerModelCredentialBinding | None:
        """Return a current self binding or None when its credential is unavailable."""
        snapshot = await self._snapshots.snapshot(access_token)
        require_personal_moss_access(
            snapshot.member,
            organization_id=snapshot.organization_id,
            owner_id=snapshot.member.owner_id,
            explicit_grant=(
                snapshot.moss_access.is_granted
                if snapshot.moss_access.source == "explicit"
                else None
            ),
        )
        return await asyncio.get_running_loop().run_in_executor(None, self._store._read, snapshot)
