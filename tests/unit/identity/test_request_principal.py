"""Actual retained HMAC credentials, canonical SQLite and separate Product DB."""

from __future__ import annotations

import dataclasses
import json
import pickle
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.encoders import jsonable_encoder
from tests.unit.identity.test_canonical_human_request_projection import (
    _AUTHORITY,
    _IDS,
    _resolver,
    _token,
)
from tests.unit.identity.test_canonical_human_request_projection import (
    legacy_db as legacy_db,
)
from tests.unit.identity.test_canonical_human_request_projection import (
    product as product,
)

from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.canonical_identity import CanonicalIdentityProjectionRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.canonical import has_signed_scope
from octop.infra.identity.projection import CanonicalRequestIdentity
from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest, authenticate


async def test_real_authentication_preserves_actor_and_private_original_expected(
    legacy_db: Path, product: SqlitePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = legacy_db.read_bytes()
    resolver = _resolver(legacy_db, product)
    principal = await authenticate(resolver, _token())
    assert isinstance(principal, CanonicalAuthenticatedRequest)
    original = principal.identity
    assert original.projection.identity.authority_id == _AUTHORITY
    assert original.projection.identity.account_id == _IDS["account"]
    assert original.projection.identity.member_id == _IDS["homeMember"]
    assert original.snapshot.member.role == "admin"
    assert type(original.projection.local_user_id) is int
    assert original.snapshot.session_id is None
    user = UserRepo(product).get(original.projection.local_user_id)
    assert user is not None and user.role == "user" and user.permissions == []
    assert user.password_hash is None
    calls: list[tuple[str, CanonicalRequestIdentity]] = []
    revalidate = resolver.revalidate

    async def record(token: str, expected: CanonicalRequestIdentity) -> CanonicalRequestIdentity:
        calls.append((token, expected))
        return await revalidate(token, expected)

    monkeypatch.setattr(resolver, "revalidate", record)
    for _ in range(2):
        current = await principal.revalidate()
        assert current == original and current is not original
        assert principal.identity is original
    assert len(calls) == 2
    assert all(token == _token() and expected is original for token, expected in calls)
    assert legacy_db.read_bytes() == before


async def test_same_account_in_two_orgs_remains_two_actual_actor_members(
    legacy_db: Path, product: SqlitePool
) -> None:
    resolver = _resolver(legacy_db, product)
    home = await authenticate(resolver, _token())
    company = await authenticate(resolver, _token("sms_company_actual_member"))
    assert home.identity.snapshot.account_id == company.identity.snapshot.account_id
    assert home.identity.snapshot.member.owner_id == _IDS["homeMember"]
    assert company.identity.snapshot.member.owner_id == _IDS["companyMember"]
    assert home.identity.projection.local_user_id != company.identity.projection.local_user_id


async def test_no_token_in_repr_vars_json_or_pickle(legacy_db: Path, product: SqlitePool) -> None:
    token = _token()
    principal = await authenticate(_resolver(legacy_db, product), token)
    assert repr(principal) == str(principal) == "CanonicalAuthenticatedRequest()"
    assert token not in repr(principal)
    assert not hasattr(principal, "__dict__")
    for operation in (
        lambda: vars(principal),
        lambda: json.dumps(principal),
        lambda: json.dumps(principal, default=vars),
        lambda: pickle.dumps(principal),
        lambda: dataclasses.asdict(principal),  # type: ignore[arg-type]
    ):
        with pytest.raises(TypeError) as caught:
            operation()
        assert token not in str(caught.value)
    with pytest.raises(ValueError) as caught_encoder:
        jsonable_encoder(principal)
    assert token not in str(caught_encoder.value)
    assert token not in json.dumps(principal, default=repr)


async def test_normal_construction_assignment_and_subclassing_are_unavailable(
    legacy_db: Path, product: SqlitePool
) -> None:
    principal = await authenticate(_resolver(legacy_db, product), _token())
    with pytest.raises(TypeError, match="authenticate"):
        CanonicalAuthenticatedRequest()
    with pytest.raises(AttributeError):
        principal.identity = principal.identity  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del principal.identity
    with pytest.raises(TypeError, match="subclassed"):
        type("ForgedPrincipal", (CanonicalAuthenticatedRequest,), {})
    # A raw object allocation has no factory provenance and is not accepted.
    fake = object.__new__(CanonicalAuthenticatedRequest)
    with pytest.raises(MossworkError) as caught:
        await fake.revalidate()
    assert caught.value.code == ErrorCode.AUTH_FAILED


@pytest.mark.parametrize("forgery", ["snapshot", "account", "foreign_tuple", "mapping", "scopes"])
async def test_even_process_internal_expected_tampering_cannot_pass_revalidation(
    legacy_db: Path, product: SqlitePool, forgery: str
) -> None:
    token = _token("admin_with_narrow_signed_scopes")
    principal = await authenticate(_resolver(legacy_db, product), token)
    original = principal.identity
    if forgery == "snapshot":
        forged = dataclasses.replace(
            original, snapshot=dataclasses.replace(original.snapshot, membership_revision=999)
        )
    elif forgery == "account":
        forged = dataclasses.replace(
            original,
            snapshot=dataclasses.replace(original.snapshot, account_id="foreign-account"),
            projection=dataclasses.replace(
                original.projection,
                identity=dataclasses.replace(
                    original.projection.identity, account_id="foreign-account"
                ),
            ),
        )
    elif forgery == "foreign_tuple":
        forged = dataclasses.replace(
            original,
            snapshot=dataclasses.replace(
                original.snapshot,
                organization_id=_IDS["companyOrg"],
                member=dataclasses.replace(
                    original.snapshot.member,
                    organization_id=_IDS["companyOrg"],
                    owner_id=_IDS["companyMember"],
                ),
            ),
            projection=dataclasses.replace(
                original.projection,
                identity=dataclasses.replace(
                    original.projection.identity,
                    organization_id=_IDS["companyOrg"],
                    member_id=_IDS["companyMember"],
                ),
            ),
        )
    elif forgery == "mapping":
        forged = dataclasses.replace(
            original, projection=dataclasses.replace(original.projection, local_user_id=999)
        )
    else:
        assert not has_signed_scope(original.snapshot.scopes, "sessions:create")
        forged = dataclasses.replace(
            original, snapshot=dataclasses.replace(original.snapshot, scopes=("*",))
        )
    # Deliberately bypass normal Python read-only access to test the required
    # real revalidation boundary, not merely dataclass/slots type checks.
    object.__setattr__(principal, "_CanonicalAuthenticatedRequest__identity", forged)
    with pytest.raises(MossworkError) as caught:
        await principal.revalidate()
    assert caught.value.code == ErrorCode.FORBIDDEN


@pytest.mark.parametrize(
    "mutation", ["grant", "revision", "account", "member", "role", "jti", "local_role"]
)
async def test_late_revocation_invalidates_original_principal(
    legacy_db: Path, product: SqlitePool, mutation: str
) -> None:
    principal = await authenticate(_resolver(legacy_db, product), _token())
    original = principal.identity
    if mutation == "local_role":
        with product.transaction() as conn:
            conn.execute("UPDATE users SET role = 'admin'")
    else:
        with sqlite3.connect(legacy_db) as conn:
            if mutation == "grant":
                conn.execute(
                    "INSERT INTO moss_member_access VALUES (?, ?, 0, ?, 4)",
                    (_IDS["homeOrg"], _IDS["homeMember"], _IDS["homeMember"]),
                )
            elif mutation == "revision":
                conn.execute("UPDATE users SET membership_revision = membership_revision + 1")
            elif mutation == "account":
                conn.execute("UPDATE human_accounts SET status = 'disabled'")
            elif mutation == "member":
                conn.execute("UPDATE users SET status = 'disabled'")
            elif mutation == "role":
                conn.execute("UPDATE users SET role = 'user'")
            else:
                conn.execute(
                    "INSERT INTO revoked_tokens VALUES (?, ?)",
                    (original.snapshot.credential_id, original.snapshot.credential_expires_at),
                )
    with pytest.raises(MossworkError) as caught:
        await principal.revalidate()
    assert caught.value.code in {ErrorCode.AUTH_FAILED, ErrorCode.FORBIDDEN}
    assert principal.identity is original


async def test_forged_product_account_association_never_creates_a_principal(
    legacy_db: Path, product: SqlitePool
) -> None:
    CanonicalIdentityProjectionRepo(product).get_or_create(
        authority_id=_AUTHORITY,
        account_id="foreign-account",
        organization_id=_IDS["homeOrg"],
        member_id=_IDS["homeMember"],
    )
    with pytest.raises(MossworkError) as caught:
        await authenticate(_resolver(legacy_db, product), _token())
    assert caught.value.code == ErrorCode.AUTH_FAILED
    assert UserRepo(product).count() == 1


@pytest.mark.parametrize(
    "name",
    [
        "foreign_org_super_admin",
        "api_key_credential",
        "internal_channel_credential",
        "control_credential",
        "refresh_token",
        "tampered_signature",
        "expired",
    ],
)
async def test_only_current_hmac_verified_human_access_can_create_principal(
    legacy_db: Path, product: SqlitePool, name: str
) -> None:
    with pytest.raises(MossworkError) as caught:
        await authenticate(_resolver(legacy_db, product), _token(name))
    assert caught.value.code == ErrorCode.AUTH_FAILED
    assert UserRepo(product).count() == 0


async def test_raw_metadata_or_resolver_shaped_object_is_not_a_factory_provider() -> None:
    class RawMetadataResolver:
        async def resolve(self, token: str) -> Any:
            raise AssertionError("A metadata provider must not be called")

    with pytest.raises(MossworkError) as caught:
        await authenticate(RawMetadataResolver(), "arbitrary")  # type: ignore[arg-type]
    assert caught.value.status == 503
