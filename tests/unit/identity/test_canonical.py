"""Read real SQLite fixtures frozen from the retained UUID identity schema."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from octop.infra.agents.personal_moss_identity import personal_moss_identity
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import (
    CanonicalSnapshotService,
    SqliteCanonicalSnapshotStore,
    VerifiedHumanPrincipal,
    has_signed_scope,
)

# sudoprivacy/moss@d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d:
# authCenter/db.ts:384-430, identity/accountMemberships.ts:4-20,
# mossAgentAccess.ts:7-15. These are the original table/constraint definitions,
# not Mosswork's unrelated integer users schema. No production DB is opened.
_FROZEN_SCHEMA = """
CREATE TABLE organizations (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE departments (
    id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
    parent_id TEXT REFERENCES departments(id), name TEXT NOT NULL,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE users (
    id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
    email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    department_id TEXT REFERENCES departments(id), role TEXT NOT NULL DEFAULT 'user',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending', 'active', 'locked', 'disabled')),
    membership_revision INTEGER NOT NULL DEFAULT 0, password_hash TEXT,
    password_updated_at INTEGER, last_login_at INTEGER, created_at INTEGER NOT NULL
);
CREATE TABLE revoked_tokens (jti TEXT PRIMARY KEY, expires_at INTEGER NOT NULL);
CREATE UNIQUE INDEX users_id_org_unique ON users (id, org_id);
CREATE TABLE human_accounts (
    id TEXT PRIMARY KEY, phone TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'locked', 'disabled')),
    created_at BIGINT NOT NULL
);
CREATE TABLE human_account_memberships (
    account_id TEXT NOT NULL REFERENCES human_accounts(id),
    org_id TEXT NOT NULL REFERENCES organizations(id), user_id TEXT NOT NULL UNIQUE,
    created_at BIGINT NOT NULL, PRIMARY KEY (account_id, org_id),
    FOREIGN KEY (user_id, org_id) REFERENCES users(id, org_id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE TABLE moss_member_access (
    org_id TEXT NOT NULL, user_id TEXT NOT NULL,
    enabled BIGINT NOT NULL CHECK (enabled IN (0, 1)), updated_by TEXT NOT NULL,
    updated_at BIGINT NOT NULL, PRIMARY KEY (org_id, user_id)
);
"""
_ACCOUNT = "9cdd9b27-a3f0-40a6-9015-8b170c237c67"
_HOME_ORG = "4ce65010-7331-415a-abaa-4d47387d845e"
_COMPANY_ORG = "d3f29104-c19b-4594-859c-a92734c82dbb"
_HOME_MEMBER = "c2cc8374-8cdf-4936-8967-d891fb500634"
_COMPANY_MEMBER = "23483f4d-daa0-44a5-8d60-09b19be380d7"
_NOW = 2_000_000_000


class _Authority:
    """Isolated substitute for the injected verifier, never a JWT decoder."""

    def __init__(self, principal: VerifiedHumanPrincipal | None) -> None:
        self.principal = principal
        self.tokens: list[str] = []

    async def verify_human(self, access_token: str) -> VerifiedHumanPrincipal | None:
        self.tokens.append(access_token)
        return self.principal


@pytest.fixture
def canonical_db(tmp_path: Path) -> Path:
    path = tmp_path / "retained-moss.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("PRAGMA foreign_keys = ON;" + _FROZEN_SCHEMA)
        connection.executemany(
            "INSERT INTO organizations VALUES (?, ?, 1)",
            [(_HOME_ORG, "Personal organization"), (_COMPANY_ORG, "Company")],
        )
        connection.executemany(
            "INSERT INTO users (id, org_id, email, name, role, status, membership_revision, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', 3, 1)",
            [
                (_HOME_MEMBER, _HOME_ORG, "home@example.test", "home", "admin"),
                (_COMPANY_MEMBER, _COMPANY_ORG, "company@example.test", "member-generated", "user"),
            ],
        )
        connection.execute(
            "INSERT INTO human_accounts VALUES (?, '13900000001', 'active', 1)", (_ACCOUNT,)
        )
        connection.executemany(
            "INSERT INTO human_account_memberships VALUES (?, ?, ?, 1)",
            [(_ACCOUNT, _HOME_ORG, _HOME_MEMBER), (_ACCOUNT, _COMPANY_ORG, _COMPANY_MEMBER)],
        )
        connection.execute(
            "INSERT INTO moss_member_access VALUES (?, ?, 1, ?, 2)",
            (_COMPANY_ORG, _COMPANY_MEMBER, _HOME_MEMBER),
        )
    return path


def _principal(**changes: object) -> VerifiedHumanPrincipal:
    original = VerifiedHumanPrincipal(
        authority_id="retained-moss",
        account_id=_ACCOUNT,
        organization_id=_HOME_ORG,
        member_id=_HOME_MEMBER,
        credential_role="admin",
        auth_app="sudowork",
        key_id="phone-login",
        credential_id="verified-access-jti",
        expires_at=_NOW + 300,
    )
    return replace(original, **changes)


def _service(
    path: Path, principal: VerifiedHumanPrincipal | None = None
) -> CanonicalSnapshotService:
    return CanonicalSnapshotService(
        SqliteCanonicalSnapshotStore(path, authority_id="retained-moss"),
        _Authority(principal or _principal()),
        clock=lambda: _NOW,
    )


async def test_preserves_one_human_and_distinct_real_company_members(canonical_db: Path) -> None:
    before = canonical_db.read_bytes()
    home = await _service(canonical_db).snapshot("opaque-verified-token")
    company = await _service(
        canonical_db,
        _principal(organization_id=_COMPANY_ORG, member_id=_COMPANY_MEMBER, credential_role="user"),
    ).snapshot("opaque-switched-token")
    assert home.account_id == company.account_id == _ACCOUNT
    assert home.member.owner_id == _HOME_MEMBER
    assert company.member.owner_id == _COMPANY_MEMBER
    assert home.organization_id != company.organization_id
    assert home.personal_agent == personal_moss_identity(_HOME_ORG, _HOME_MEMBER)
    assert company.personal_agent == personal_moss_identity(_COMPANY_ORG, _COMPANY_MEMBER)
    assert company.membership_revision == 3
    assert company.moss_access.source == "explicit"
    assert company.grant_updated_at == 2
    assert home.session_id is None
    assert home.credential_id == "verified-access-jti"
    assert home.credential_expires_at == _NOW + 300
    assert home.scopes == ()
    assert not has_signed_scope(home.scopes, "sessions:create")
    assert home.auth_app == "sudowork"
    assert home.key_id == "phone-login"
    assert canonical_db.read_bytes() == before


@pytest.mark.parametrize("status", ["locked", "disabled"])
async def test_account_revocation_rejects_every_membership(canonical_db: Path, status: str) -> None:
    with sqlite3.connect(canonical_db) as connection:
        connection.execute("UPDATE human_accounts SET status = ?", (status,))
    for principal in [
        _principal(),
        _principal(organization_id=_COMPANY_ORG, member_id=_COMPANY_MEMBER, credential_role="user"),
    ]:
        with pytest.raises(MossworkError) as caught:
            await _service(canonical_db, principal).snapshot("opaque-token")
        assert caught.value.code == ErrorCode.AUTH_FAILED


@pytest.mark.parametrize("status", ["pending", "locked", "disabled"])
async def test_member_inactive_rejects_snapshot(canonical_db: Path, status: str) -> None:
    with sqlite3.connect(canonical_db) as connection:
        connection.execute("UPDATE users SET status = ? WHERE id = ?", (status, _HOME_MEMBER))
    with pytest.raises(MossworkError) as caught:
        await _service(canonical_db).snapshot("opaque-token")
    assert caught.value.status == 401


async def test_explicit_admin_revocation_is_current_and_has_no_agent(canonical_db: Path) -> None:
    service = _service(canonical_db)
    assert (await service.snapshot("opaque-token")).moss_access.is_enabled
    with sqlite3.connect(canonical_db) as connection:
        connection.execute(
            "INSERT INTO moss_member_access VALUES (?, ?, 0, ?, 8)",
            (_HOME_ORG, _HOME_MEMBER, _HOME_MEMBER),
        )
    snapshot = await service.snapshot("same-token")
    assert not snapshot.moss_access.is_granted
    assert not snapshot.moss_access.is_enabled
    assert snapshot.moss_access.source == "explicit"
    assert snapshot.grant_updated_at == 8
    assert snapshot.personal_agent is None


@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": _HOME_MEMBER},
        {"organization_id": _COMPANY_ORG},
        {"member_id": _COMPANY_MEMBER},
        {"credential_role": "super_admin"},
        {"authority_id": "another-authority"},
        {"key_id": "an-api-key-id"},
        {"key_id": "internal:channel"},
        {"auth_app": "moss"},
        {"expires_at": _NOW},
    ],
)
async def test_mismatched_proofs_never_acquire_admin_target_access(
    canonical_db: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(MossworkError) as caught:
        await _service(canonical_db, _principal(**changes)).snapshot("opaque-token")
    assert caught.value.code == ErrorCode.AUTH_FAILED


async def test_role_change_and_jti_revocation_are_rechecked(canonical_db: Path) -> None:
    service = _service(canonical_db)
    await service.snapshot("opaque-token")
    with sqlite3.connect(canonical_db) as connection:
        connection.execute(
            "INSERT INTO revoked_tokens VALUES ('verified-access-jti', ?)", (_NOW + 300,)
        )
    with pytest.raises(MossworkError) as caught:
        await service.snapshot("same-token")
    assert caught.value.status == 401
    with sqlite3.connect(canonical_db) as connection:
        connection.execute("DELETE FROM revoked_tokens")
        connection.execute("UPDATE users SET role = 'user' WHERE id = ?", (_HOME_MEMBER,))
    with pytest.raises(MossworkError) as caught:
        await service.snapshot("same-token")
    assert caught.value.status == 401


async def test_missing_store_is_unavailable_and_does_not_create_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite"
    with pytest.raises(MossworkError) as caught:
        await _service(path).snapshot("opaque-token")
    assert caught.value.code == ErrorCode.INTERNAL_ERROR
    assert caught.value.status == 503
    assert not path.exists()


async def test_unrelated_integer_user_schema_is_not_migrated(tmp_path: Path) -> None:
    path = tmp_path / "mosswork.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    before = path.read_bytes()
    with pytest.raises(MossworkError) as caught:
        await _service(path).snapshot("opaque-token")
    assert caught.value.status == 503
    assert path.read_bytes() == before


async def test_removed_membership_is_not_repaired_or_recreated(canonical_db: Path) -> None:
    with sqlite3.connect(canonical_db) as connection:
        connection.execute(
            "DELETE FROM human_account_memberships WHERE user_id = ?", (_HOME_MEMBER,)
        )
    before = canonical_db.read_bytes()
    with pytest.raises(MossworkError) as caught:
        await _service(canonical_db).snapshot("opaque-token")
    assert caught.value.status == 401
    assert canonical_db.read_bytes() == before


async def test_foreign_org_super_admin_is_not_a_target_member(canonical_db: Path) -> None:
    with sqlite3.connect(canonical_db) as connection:
        connection.execute("UPDATE users SET role = 'super_admin' WHERE id = ?", (_HOME_MEMBER,))
    principal = _principal(organization_id=_COMPANY_ORG, credential_role="super_admin")
    with pytest.raises(MossworkError) as caught:
        await _service(canonical_db, principal).snapshot("opaque-admin-scoped-token")
    assert caught.value.status == 401


async def test_real_authority_session_is_preserved_separately_from_credential(
    canonical_db: Path,
) -> None:
    snapshot = await _service(
        canonical_db, _principal(session_id="verified-authority-session")
    ).snapshot("opaque-token")
    assert snapshot.session_id == "verified-authority-session"
    assert snapshot.credential_id == "verified-access-jti"


async def test_malformed_grant_is_unavailable_not_a_denial(canonical_db: Path) -> None:
    with sqlite3.connect(canonical_db) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE moss_member_access SET enabled = 2")
    principal = _principal(
        organization_id=_COMPANY_ORG, member_id=_COMPANY_MEMBER, credential_role="user"
    )
    with pytest.raises(MossworkError) as caught:
        await _service(canonical_db, principal).snapshot("opaque-token")
    assert caught.value.status == 503


async def test_http_fields_are_not_promoted_to_a_verified_principal(canonical_db: Path) -> None:
    class RequestFieldsAuthority:
        async def verify_human(self, access_token: str) -> VerifiedHumanPrincipal | None:
            return {"account_id": _ACCOUNT, "member_id": _HOME_MEMBER}  # type: ignore[return-value]

    service = CanonicalSnapshotService(
        SqliteCanonicalSnapshotStore(canonical_db, authority_id="retained-moss"),
        RequestFieldsAuthority(),
    )
    with pytest.raises(MossworkError) as caught:
        await service.snapshot("opaque-token")
    assert caught.value.status == 401


async def test_mutable_scope_container_cannot_become_a_capability(canonical_db: Path) -> None:
    scopes = ["*"]
    with pytest.raises(MossworkError) as caught:
        await _service(canonical_db, _principal(scopes=scopes)).snapshot("opaque-token")
    assert caught.value.status == 401
    assert not has_signed_scope(scopes, "sessions:create")  # type: ignore[arg-type]


async def test_authority_failure_is_unavailable_not_absent(canonical_db: Path) -> None:
    class UnavailableAuthority:
        async def verify_human(self, access_token: str) -> VerifiedHumanPrincipal | None:
            raise ConnectionError("authority unavailable")

    service = CanonicalSnapshotService(
        SqliteCanonicalSnapshotStore(canonical_db, authority_id="retained-moss"),
        UnavailableAuthority(),
    )
    with pytest.raises(MossworkError) as caught:
        await service.snapshot("opaque-token")
    assert caught.value.status == 503
    assert caught.value.details == {"dependency": "canonical_authority"}


async def test_invalid_authority_credential_is_rejected_before_opening_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.sqlite"
    authority = _Authority(None)
    service = CanonicalSnapshotService(
        SqliteCanonicalSnapshotStore(path, authority_id="retained-moss"), authority
    )
    with pytest.raises(MossworkError) as caught:
        await service.snapshot("opaque-invalid-token")
    assert caught.value.status == 401
    assert authority.tokens == ["opaque-invalid-token"]
    assert not path.exists()


async def test_sqlite_work_runs_outside_event_loop(
    canonical_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_loop_thread = threading.get_ident()
    observed_threads: list[int] = []
    original = sqlite3.connect

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        observed_threads.append(threading.get_ident())
        return original(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(sqlite3, "connect", connect)
    await _service(canonical_db).snapshot("opaque-token")
    assert observed_threads and all(value != event_loop_thread for value in observed_threads)
    assert asyncio.get_running_loop().is_running()


def test_postgresql_url_is_not_silently_treated_as_sqlite() -> None:
    with pytest.raises(TypeError, match="PostgreSQL"):
        SqliteCanonicalSnapshotStore("postgresql://example/moss", authority_id="retained-moss")  # type: ignore[arg-type]
