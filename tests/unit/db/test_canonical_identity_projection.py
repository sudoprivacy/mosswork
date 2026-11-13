"""Product allocator and atomic canonical compatibility mappings."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from octop.infra.db import migrate
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos import canonical_identity
from octop.infra.db.repos.canonical_identity import (
    CanonicalIdentityProjectionRepo,
    CanonicalProjectionConflict,
    CanonicalProjectionUserChanged,
)
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.retention import RetainedResourceError

_KEY = {
    "authority_id": "retained-moss",
    "account_id": "9cdd9b27-a3f0-40a6-9015-8b170c237c67",
    "organization_id": "4ce65010-7331-415a-abaa-4d47387d845e",
    "member_id": "c2cc8374-8cdf-4936-8967-d891fb500634",
}


@pytest.fixture
def product(tmp_path: Path) -> Iterator[SqlitePool]:
    pool = SqlitePool(tmp_path / "product.sqlite")
    run_migrations(pool)
    try:
        yield pool
    finally:
        pool.close()


def test_fresh_and_existing_v30_product_migrations_preserve_bindings(product: SqlitePool) -> None:
    repo = CanonicalIdentityProjectionRepo(product)
    expected = repo.get_or_create(**_KEY)
    run_migrations(product)
    run_migrations(product)
    assert repo.get(**_KEY) == expected
    assert UserRepo(product).count() == 1
    with product.connect() as conn:
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 30
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('human_accounts', 'human_account_memberships', 'moss_member_access')"
        ).fetchall()


@pytest.mark.parametrize("prior_version", [29, 30])
def test_missing_projection_schema_is_repaired_without_version_bump(
    product: SqlitePool, prior_version: int
) -> None:
    with product.transaction() as conn:
        conn.execute("DROP TABLE canonical_user_projections")
        conn.execute("UPDATE _schema_version SET version = ?", (prior_version,))
    run_migrations(product)
    run_migrations(product)
    assert CanonicalIdentityProjectionRepo(product).get(**_KEY) is None
    with product.connect() as conn:
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 30


def test_postgresql_and_sqlite_use_same_idempotent_v30_binding_constraints() -> None:
    statements: list[str] = []

    class RecordingConnection:
        def execute(self, sql: str) -> None:
            statements.append(sql)

    class RecordingPostgresPool:
        dialect = "postgresql"

        @contextmanager
        def transaction(self) -> Iterator[RecordingConnection]:
            yield RecordingConnection()

        @contextmanager
        def connect(self) -> Iterator[Any]:
            raise AssertionError("The projection repair owns one transaction")
            yield

        def close(self) -> None:
            pass

    migrate._ensure_canonical_product_projection_schema(RecordingPostgresPool())
    assert len(statements) == 1
    ddl = statements[0]
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS canonical_user_projections")
    assert "PRIMARY KEY (authority_id, organization_id, member_id)" in ddl
    assert "local_user_id BIGINT NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT" in ddl
    assert "UPDATE _schema_version" not in ddl
    assert "human_accounts" not in ddl
    sqlite_sql = (migrate._MIGRATIONS_DIR / "030_employee_knowledge.sql").read_text(
        encoding="utf-8"
    )
    assert ddl in sqlite_sql
    assert max(version for version, _ in migrate._discover("postgresql")) == 30
    assert len({version for version, _ in migrate._discover("postgresql")}) == len(
        migrate._discover("postgresql")
    )


def test_two_pool_instances_and_threads_allocate_exactly_one_user(product: SqlitePool) -> None:
    second = SqlitePool(product.path)
    try:
        repos = [CanonicalIdentityProjectionRepo(product), CanonicalIdentityProjectionRepo(second)]
        start = threading.Barrier(8)

        def resolve(index: int) -> int:
            start.wait(timeout=10)
            return repos[index % 2].get_or_create(**_KEY).local_user_id

        with ThreadPoolExecutor(max_workers=8) as workers:
            ids = list(workers.map(resolve, range(8)))
        assert len(set(ids)) == 1
        assert repos[0].get(**_KEY) == repos[1].get(**_KEY)
        assert UserRepo(product).count() == 1
        with product.connect() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM canonical_user_projections").fetchone()[0] == 1
            )
    finally:
        second.close()


def test_same_account_different_orgs_or_authorities_have_distinct_ids(product: SqlitePool) -> None:
    repo = CanonicalIdentityProjectionRepo(product)
    personal = repo.get_or_create(**_KEY)
    company = repo.get_or_create(
        **{
            **_KEY,
            "organization_id": "d3f29104-c19b-4594-859c-a92734c82dbb",
            "member_id": "23483f4d-daa0-44a5-8d60-09b19be380d7",
        }
    )
    other_authority = repo.get_or_create(**{**_KEY, "authority_id": "another-authority"})
    assert len({personal.local_user_id, company.local_user_id, other_authority.local_user_id}) == 3
    assert personal.account_id == company.account_id == other_authority.account_id


def test_account_mismatch_never_rebinds_or_allocates(product: SqlitePool) -> None:
    repo = CanonicalIdentityProjectionRepo(product)
    expected = repo.get_or_create(**_KEY)
    for operation in (repo.get, repo.get_or_create):
        with pytest.raises(CanonicalProjectionConflict):
            operation(**{**_KEY, "account_id": "different-canonical-account"})
    assert repo.get(**_KEY) == expected
    assert UserRepo(product).count() == 1


def test_standalone_username_collision_allocates_another_user(
    product: SqlitePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    users = UserRepo(product)
    standalone = users.create(
        username="canonical_collision", password_hash="standalone-password", role="admin"
    )
    original = users.get(standalone)
    names = iter(["collision", "fresh-product-id"])
    monkeypatch.setattr(canonical_identity, "new_ulid", lambda: next(names))
    projection = CanonicalIdentityProjectionRepo(product).get_or_create(**_KEY)
    assert projection.local_user_id != standalone
    assert users.get(standalone) == original
    assert users.get(projection.local_user_id).username == "canonical_fresh-product-id"
    assert users.count() == 2


def test_projection_insert_failure_rolls_back_user_and_trigger_side_effects(
    product: SqlitePool,
) -> None:
    with product.connect() as conn:
        conn.execute(
            "CREATE TRIGGER reject_projection BEFORE INSERT ON canonical_user_projections "
            "BEGIN SELECT RAISE(ABORT, 'fixture rejects mapping'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture rejects mapping"):
        CanonicalIdentityProjectionRepo(product).get_or_create(**_KEY)
    assert UserRepo(product).count() == 0
    with product.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM canonical_user_projections").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM communication_identities").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM communication_identity_outbox").fetchone()[0] == 0


def test_local_user_is_unique_and_cannot_be_deleted_while_bound(product: SqlitePool) -> None:
    projection = CanonicalIdentityProjectionRepo(product).get_or_create(**_KEY)
    with pytest.raises(sqlite3.IntegrityError), product.transaction() as conn:
        conn.execute(
            "INSERT INTO canonical_user_projections VALUES (?, ?, ?, ?, ?, 1)",
            (
                "other",
                _KEY["account_id"],
                "another-org",
                "another-member",
                projection.local_user_id,
            ),
        )
    with pytest.raises(RetainedResourceError):
        UserRepo(product).delete(projection.local_user_id)
    # The explicit domain refusal does not replace the original database FK guard.
    with pytest.raises(sqlite3.IntegrityError), product.transaction() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (projection.local_user_id,))
    assert CanonicalIdentityProjectionRepo(product).get(**_KEY) == projection


@pytest.mark.parametrize(
    "update",
    [
        "role = 'admin'",
        "password_hash = 'local-password'",
        "permissions = '[\"admin.users\"]'",
        "permissions = '{}'",
        "disabled = 1",
        "sso_subject = 'local-login'",
        "expert_access_mode = 'legacy'",
    ],
)
def test_local_authority_changes_fail_closed(product: SqlitePool, update: str) -> None:
    repo = CanonicalIdentityProjectionRepo(product)
    projection = repo.get_or_create(**_KEY)
    with product.transaction() as conn:
        conn.execute(f"UPDATE users SET {update} WHERE id = ?", (projection.local_user_id,))
    for operation in (repo.get, repo.get_or_create):
        with pytest.raises(CanonicalProjectionUserChanged):
            operation(**_KEY)
    assert UserRepo(product).count() == 1


@pytest.mark.parametrize("role", ["user", "admin"])
@pytest.mark.parametrize("configured_source", [False, True])
def test_original_create_and_transaction_allocator_keep_original_defaults(
    product: SqlitePool, role: str, configured_source: bool
) -> None:
    users = UserRepo(product)
    if configured_source:
        with product.transaction() as conn:
            conn.execute("UPDATE expert_assignment_source SET revision = 1 WHERE id = 1")
    normal = users.create(username="normal-create", role=role)
    with product.transaction() as conn:
        borrowed = users.create_in_transaction(conn, username="transaction-create", role=role)
    first, second = users.get(normal), users.get(borrowed)
    assert first is not None and second is not None
    assert (
        replace(first, id=second.id, username=second.username, created_at=second.created_at)
        == second
    )
    assert first.password_hash is None
    assert first.permissions == []
    assert first.role == role
    with product.connect() as conn:
        rows = conn.execute("SELECT expert_access_mode FROM users ORDER BY id").fetchall()
    expected = "assigned_only" if configured_source and role != "admin" else "legacy"
    assert [row[0] for row in rows] == [expected, expected]


def test_transaction_allocator_preserves_supplied_fields_and_rollback(product: SqlitePool) -> None:
    users = UserRepo(product)
    with product.transaction() as conn:
        user_id = users.create_in_transaction(
            conn,
            username="explicit-fields",
            password_hash="public-test-hash",
            role="user",
            display_name="Standalone profile",
            locale="en",
            email="standalone@example.test",
            permissions=["models.read"],
            role_name="Employee",
        )
    row = users.get(user_id)
    assert row is not None
    assert row.password_hash == "public-test-hash"
    assert row.display_name == "Standalone profile"
    assert row.locale == "en"
    assert row.email == "standalone@example.test"
    assert row.permissions == ["models.read"]
    assert row.role_name == "Employee"
    with pytest.raises(RuntimeError), product.transaction() as conn:
        users.create_in_transaction(conn, username="rolled-back", role="user")
        raise RuntimeError("rollback owned transaction")
    assert users.get_by_username("rolled-back") is None
