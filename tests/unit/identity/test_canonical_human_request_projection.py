"""Canonical opaque human credentials and fresh product projections."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from tests.unit.identity.test_human_tokens import legacy_db as legacy_db

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.canonical_identity import CanonicalIdentityProjectionRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import (
    CanonicalIdentitySnapshot,
    CanonicalSnapshotService,
    SqliteCanonicalSnapshotStore,
    has_signed_scope,
)
from octop.infra.identity.human_tokens import CanonicalHumanTokenVerifier
from octop.infra.identity.projection import (
    CanonicalIdentityKey,
    CanonicalLocalUserProjection,
    CanonicalRequestIdentity,
    CanonicalRequestIdentityResolver,
)

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "human_tokens_legacy.json").read_text(encoding="utf-8")
)
_IDS = _FIXTURE["ids"]
_NOW = _FIXTURE["verificationTime"]
_AUTHORITY = "retained-moss"


def _token(name: str = "sms_home") -> str:
    return next(case["token"] for case in _FIXTURE["cases"] if case["name"] == name)


@pytest.fixture
def product(tmp_path: Path) -> Iterator[SqlitePool]:
    pool = SqlitePool(tmp_path / "separate-product.sqlite")
    run_migrations(pool)
    try:
        yield pool
    finally:
        pool.close()


def _snapshots(path: Path) -> CanonicalSnapshotService:
    return CanonicalSnapshotService(
        SqliteCanonicalSnapshotStore(path, authority_id=_AUTHORITY),
        CanonicalHumanTokenVerifier(
            path,
            authority_id=_AUTHORITY,
            secret=_FIXTURE["dummySecret"],
            issuer=_FIXTURE["issuer"],
            expected_audience=None,
            clock=lambda: _NOW,
        ),
        clock=lambda: _NOW,
    )


def _resolver(path: Path, product: SqlitePool) -> CanonicalRequestIdentityResolver:
    return CanonicalRequestIdentityResolver(
        _snapshots(path), CanonicalIdentityProjectionRepo(product)
    )


async def test_real_human_multiorg_projection_preserves_uuid_and_writes_only_product(
    legacy_db: Path, product: SqlitePool
) -> None:
    before = legacy_db.read_bytes()
    resolver = _resolver(legacy_db, product)
    personal = await resolver.resolve(_token())
    company = await resolver.resolve(_token("sms_company_actual_member"))
    current = await resolver.revalidate(_token(), personal)
    assert current == personal and current is not personal
    assert personal.projection.identity == CanonicalIdentityKey(
        _AUTHORITY, _IDS["account"], _IDS["homeOrg"], _IDS["homeMember"]
    )
    assert company.projection.identity.account_id == personal.projection.identity.account_id
    assert company.projection.identity.member_id == _IDS["companyMember"]
    assert company.projection.local_user_id != personal.projection.local_user_id
    assert type(personal.projection.local_user_id) is int
    assert personal.snapshot.member.role == "admin"
    for projection in (personal.projection, company.projection):
        row = UserRepo(product).get(projection.local_user_id)
        assert row is not None
        assert row.role == "user"
        assert row.password_hash is None
        assert row.permissions == []
        assert row.email is row.sso_provider_id is row.sso_subject is None
    with product.connect() as conn:
        for table in (
            "agents",
            "sessions",
            "threads",
            "user_sso_identities",
            "employee_knowledge_bases",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert legacy_db.read_bytes() == before
    with sqlite3.connect(legacy_db) as conn:
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'canonical_user_projections'"
        ).fetchall()


async def test_readonly_snapshot_does_not_allocate_product_projection(
    legacy_db: Path, product: SqlitePool
) -> None:
    await _snapshots(legacy_db).snapshot(_token())
    assert UserRepo(product).count() == 0
    with product.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM canonical_user_projections").fetchone()[0] == 0


async def test_existing_standalone_profile_is_never_linked_to_canonical_member(
    legacy_db: Path, product: SqlitePool
) -> None:
    users = UserRepo(product)
    standalone = users.create(
        username=_IDS["homeMember"],
        role="admin",
        password_hash="standalone-only",
        display_name="home",
        email="home@example.test",
    )
    original = users.get(standalone)
    current = await _resolver(legacy_db, product).resolve(_token())
    assert current.projection.local_user_id != standalone
    assert users.get(standalone) == original
    assert users.count() == 2


async def test_product_mapping_account_mismatch_is_refused_without_rebinding(
    legacy_db: Path, product: SqlitePool
) -> None:
    repo = CanonicalIdentityProjectionRepo(product)
    wrong = repo.get_or_create(
        authority_id=_AUTHORITY,
        account_id="different-canonical-account",
        organization_id=_IDS["homeOrg"],
        member_id=_IDS["homeMember"],
    )
    before = legacy_db.read_bytes()
    with pytest.raises(MossworkError) as caught:
        await CanonicalRequestIdentityResolver(_snapshots(legacy_db), repo).resolve(_token())
    assert caught.value.code == ErrorCode.AUTH_FAILED
    assert UserRepo(product).count() == 1
    with product.connect() as conn:
        row = conn.execute("SELECT * FROM canonical_user_projections").fetchone()
    assert row["account_id"] == wrong.account_id
    assert row["local_user_id"] == wrong.local_user_id
    assert legacy_db.read_bytes() == before


@pytest.mark.parametrize(
    "name",
    [
        "api_key_credential",
        "internal_channel_credential",
        "control_credential",
        "refresh_token",
        "foreign_org_super_admin",
    ],
)
async def test_machine_or_foreign_credentials_never_allocate(
    legacy_db: Path, product: SqlitePool, name: str
) -> None:
    with pytest.raises(MossworkError) as caught:
        await _resolver(legacy_db, product).resolve(_token(name))
    assert caught.value.code == ErrorCode.AUTH_FAILED
    assert UserRepo(product).count() == 0


@pytest.mark.parametrize(
    "mutation", ["grant", "grant_revision", "grant_timestamp", "membership_revision", "role", "jti"]
)
async def test_revalidate_invalidates_previous_decisions(
    legacy_db: Path, product: SqlitePool, mutation: str
) -> None:
    resolver = _resolver(legacy_db, product)
    if mutation == "grant_timestamp":
        with sqlite3.connect(legacy_db) as conn:
            conn.execute(
                "INSERT INTO moss_member_access VALUES (?, ?, 1, ?, 3)",
                (_IDS["homeOrg"], _IDS["homeMember"], _IDS["homeMember"]),
            )
    expected = await resolver.resolve(_token())
    with sqlite3.connect(legacy_db) as conn:
        if mutation in {"grant", "grant_revision"}:
            conn.execute(
                "INSERT INTO moss_member_access VALUES (?, ?, ?, ?, 3)",
                (_IDS["homeOrg"], _IDS["homeMember"], int(mutation != "grant"), _IDS["homeMember"]),
            )
        elif mutation == "grant_timestamp":
            conn.execute("UPDATE moss_member_access SET updated_at = updated_at + 1")
        elif mutation == "membership_revision":
            conn.execute("UPDATE users SET membership_revision = membership_revision + 1")
        elif mutation == "role":
            conn.execute("UPDATE users SET role = 'user' WHERE id = ?", (_IDS["homeMember"],))
        else:
            conn.execute(
                "INSERT INTO revoked_tokens VALUES (?, ?)",
                (expected.snapshot.credential_id, expected.snapshot.credential_expires_at),
            )
    with pytest.raises(MossworkError) as caught:
        await resolver.revalidate(_token(), expected)
    assert caught.value.code in {ErrorCode.FORBIDDEN, ErrorCode.AUTH_FAILED}
    assert UserRepo(product).count() == 1


async def test_signed_scope_change_invalidates_expected_but_never_expands_role(
    legacy_db: Path, product: SqlitePool
) -> None:
    resolver = _resolver(legacy_db, product)
    expected = await resolver.resolve(_token())
    narrow = _token("admin_with_narrow_signed_scopes")
    with pytest.raises(MossworkError) as caught:
        await resolver.revalidate(narrow, expected)
    assert caught.value.code == ErrorCode.FORBIDDEN
    current = await resolver.resolve(narrow)
    assert current.projection == expected.projection
    assert not has_signed_scope(current.snapshot.scopes, "sessions:create")
    assert isinstance(current.snapshot.scopes, tuple)


async def test_request_identity_is_not_moss_entitlement(
    legacy_db: Path, product: SqlitePool
) -> None:
    with sqlite3.connect(legacy_db) as conn:
        conn.execute(
            "INSERT INTO moss_member_access VALUES (?, ?, 0, ?, 3)",
            (_IDS["homeOrg"], _IDS["homeMember"], _IDS["homeMember"]),
        )
    current = await _resolver(legacy_db, product).resolve(_token())
    assert current.snapshot.member.role == "admin"
    assert not current.snapshot.moss_access.is_enabled
    assert UserRepo(product).get(current.projection.local_user_id).role == "user"


@pytest.mark.parametrize("after_read", ["allocation", "mapping_recheck"])
@pytest.mark.parametrize("mutation", ["grant", "account_disabled"])
async def test_awaited_product_work_is_followed_by_fresh_canonical_proof(
    legacy_db: Path,
    product: SqlitePool,
    monkeypatch: pytest.MonkeyPatch,
    after_read: str,
    mutation: str,
) -> None:
    repo = CanonicalIdentityProjectionRepo(product)
    method = "get_or_create" if after_read == "allocation" else "get"
    original = getattr(repo, method)

    def revoke_after_read(**kwargs: Any) -> Any:
        result = original(**kwargs)
        with sqlite3.connect(legacy_db) as conn:
            if mutation == "grant":
                conn.execute(
                    "INSERT INTO moss_member_access VALUES (?, ?, 0, ?, 3)",
                    (_IDS["homeOrg"], _IDS["homeMember"], _IDS["homeMember"]),
                )
            else:
                conn.execute("UPDATE human_accounts SET status = 'disabled'")
        return result

    monkeypatch.setattr(repo, method, revoke_after_read)
    with pytest.raises(MossworkError) as caught:
        await CanonicalRequestIdentityResolver(_snapshots(legacy_db), repo).resolve(_token())
    assert caught.value.code == (
        ErrorCode.FORBIDDEN if mutation == "grant" else ErrorCode.AUTH_FAILED
    )
    # A compatibility row may remain after revocation; it carries no grant or credential.
    assert UserRepo(product).count() == 1


async def test_mapping_is_fresh_after_awaited_authority_recheck(
    legacy_db: Path, product: SqlitePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = _snapshots(legacy_db)
    original = snapshots.snapshot
    calls = 0

    async def elevate_after_snapshot(token: str) -> CanonicalIdentitySnapshot:
        nonlocal calls
        result = await original(token)
        calls += 1
        if calls == 2:
            with product.transaction() as conn:
                conn.execute("UPDATE users SET role = 'admin'")
        return result

    monkeypatch.setattr(snapshots, "snapshot", elevate_after_snapshot)
    with pytest.raises(MossworkError) as caught:
        await CanonicalRequestIdentityResolver(
            snapshots, CanonicalIdentityProjectionRepo(product)
        ).resolve(_token())
    assert caught.value.code == ErrorCode.FORBIDDEN


@pytest.mark.parametrize(
    "update", ["role = 'admin'", "password_hash = 'local-login'", "permissions = '[\"*\"]'"]
)
async def test_local_authority_never_preserves_canonical_access(
    legacy_db: Path, product: SqlitePool, update: str
) -> None:
    resolver = _resolver(legacy_db, product)
    expected = await resolver.resolve(_token())
    with product.transaction() as conn:
        conn.execute(
            f"UPDATE users SET {update} WHERE id = ?", (expected.projection.local_user_id,)
        )
    for operation in (resolver.resolve(_token()), resolver.revalidate(_token(), expected)):
        with pytest.raises(MossworkError) as caught:
            await operation
        assert caught.value.code == ErrorCode.FORBIDDEN


async def test_revalidate_does_not_allocate_or_accept_changed_local_mapping(
    legacy_db: Path, product: SqlitePool
) -> None:
    resolver = _resolver(legacy_db, product)
    expected = await resolver.resolve(_token())
    with product.transaction() as conn:
        conn.execute("DELETE FROM canonical_user_projections")
    with pytest.raises(MossworkError) as caught:
        await resolver.revalidate(_token(), expected)
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert UserRepo(product).count() == 1


async def test_revalidate_refuses_replaced_local_id_even_when_user_is_unprivileged(
    legacy_db: Path, product: SqlitePool
) -> None:
    resolver = _resolver(legacy_db, product)
    expected = await resolver.resolve(_token())
    another = UserRepo(product).create(username="another-product-user", role="user")
    with product.transaction() as conn:
        conn.execute(
            "UPDATE users SET expert_access_mode = 'assigned_only' WHERE id = ?", (another,)
        )
        conn.execute("UPDATE canonical_user_projections SET local_user_id = ?", (another,))
    with pytest.raises(MossworkError) as caught:
        await resolver.revalidate(_token(), expected)
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert UserRepo(product).count() == 2


async def test_request_objects_are_frozen_and_separate_from_local_user(
    legacy_db: Path, product: SqlitePool
) -> None:
    current = await _resolver(legacy_db, product).resolve(_token())
    assert isinstance(current, CanonicalRequestIdentity)
    assert isinstance(current.projection, CanonicalLocalUserProjection)
    with pytest.raises(FrozenInstanceError):
        current.projection.local_user_id = 42  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        current.projection.identity.account_id = "another"  # type: ignore[misc]
    with pytest.raises(ValueError):
        CanonicalLocalUserProjection(current.projection.identity, True)


async def test_missing_product_schema_fails_closed_without_canonical_write(
    legacy_db: Path, tmp_path: Path
) -> None:
    product = SqlitePool(tmp_path / "not-initialized-product.sqlite")
    before = legacy_db.read_bytes()
    try:
        with pytest.raises(MossworkError) as caught:
            await _resolver(legacy_db, product).resolve(_token())
        assert caught.value.status == 503
        assert caught.value.details == {"dependency": "canonical_product_projection"}
        with product.connect() as conn:
            assert not conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'canonical_user_projections'"
            ).fetchall()
        assert legacy_db.read_bytes() == before
    finally:
        product.close()


async def test_all_product_repo_io_runs_off_event_loop(
    legacy_db: Path, product: SqlitePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = CanonicalIdentityProjectionRepo(product)
    foreground = threading.get_ident()
    observed: list[int] = []
    original_create, original_get = repo.get_or_create, repo.get

    def create(**kwargs: Any) -> Any:
        observed.append(threading.get_ident())
        return original_create(**kwargs)

    def get(**kwargs: Any) -> Any:
        observed.append(threading.get_ident())
        return original_get(**kwargs)

    monkeypatch.setattr(repo, "get_or_create", create)
    monkeypatch.setattr(repo, "get", get)
    resolver = CanonicalRequestIdentityResolver(_snapshots(legacy_db), repo)
    expected = await resolver.resolve(_token())
    await resolver.revalidate(_token(), expected)
    assert len(observed) == 4
    assert foreground not in observed
