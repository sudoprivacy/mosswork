"""Fresh original human credentials constrained by existing product actor rows."""

from __future__ import annotations

import pickle
from dataclasses import replace

import pytest
from tests.unit.identity.test_canonical_human_request_projection import product as product
from tests.unit.identity.test_human_sessions import _IDS, _Environment, _login, _write
from tests.unit.identity.test_human_sessions import legacy_db as legacy_db
from tests.unit.identity.test_human_sessions import sessions as sessions

from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.canonical_identity import CanonicalIdentityProjectionRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.errors import MossworkError
from octop.infra.identity.projection import (
    CanonicalIdentityKey,
    CanonicalLocalUserProjection,
    CanonicalRequestIdentityResolver,
)
from octop.infra.identity.request_principal import authenticate, reauthenticate


def _resolver(environment: _Environment, product: SqlitePool) -> CanonicalRequestIdentityResolver:
    return CanonicalRequestIdentityResolver(
        environment.service.snapshots, CanonicalIdentityProjectionRepo(product)
    )


async def test_fresh_refresh_credential_reauthenticates_same_actor_without_reusing_old_jti(
    sessions: _Environment, product: SqlitePool
) -> None:
    pair = await _login(sessions)
    resolver = _resolver(sessions, product)
    original = await authenticate(resolver, pair.access_token)
    refreshed = await sessions.service.refresh(pair.refresh_token)
    renewed = await reauthenticate(resolver, refreshed.access_token, original.identity.projection)
    assert renewed.identity.projection == original.identity.projection
    assert renewed.identity.snapshot.credential_id != original.identity.snapshot.credential_id
    assert await renewed.revalidate() == renewed.identity
    assert await original.revalidate() == original.identity
    assert UserRepo(product).count() == 1
    with pytest.raises(TypeError):
        pickle.dumps(renewed)
    assert refreshed.access_token not in repr(renewed)


async def test_logout_then_normal_login_requires_new_live_proof_for_original_actor(
    sessions: _Environment, product: SqlitePool
) -> None:
    pair = await _login(sessions)
    resolver = _resolver(sessions, product)
    original = await authenticate(resolver, pair.access_token)
    await sessions.service.logout(pair.access_token, pair.refresh_token)
    with pytest.raises(MossworkError):
        await original.revalidate()
    with pytest.raises(MossworkError):
        await reauthenticate(resolver, pair.access_token, original.identity.projection)
    sessions.now[0] += 61
    next_login = await _login(sessions)
    renewed = await reauthenticate(resolver, next_login.access_token, original.identity.projection)
    assert renewed.identity.snapshot.credential_id != original.identity.snapshot.credential_id
    assert renewed.identity.projection == original.identity.projection
    assert UserRepo(product).count() == 1


async def test_current_role_revision_and_signed_scopes_come_from_new_verified_credential(
    sessions: _Environment, product: SqlitePool
) -> None:
    pair = await _login(sessions)
    resolver = _resolver(sessions, product)
    original = await authenticate(resolver, pair.access_token)
    assert original.identity.snapshot.member.role == "admin"
    _write(
        sessions,
        "UPDATE users SET role='user',membership_revision=membership_revision+1 WHERE id=?",
        (_IDS["homeMember"],),
    )
    refreshed = await sessions.service.refresh(pair.refresh_token)
    renewed = await reauthenticate(resolver, refreshed.access_token, original.identity.projection)
    assert renewed.identity.snapshot.member.role == "user"
    assert (
        renewed.identity.snapshot.membership_revision
        != original.identity.snapshot.membership_revision
    )
    assert renewed.identity.snapshot.scopes != original.identity.snapshot.scopes
    assert "*" not in renewed.identity.snapshot.scopes
    with pytest.raises(MossworkError):
        await original.revalidate()
    with pytest.raises(MossworkError):
        await resolver.revalidate(refreshed.access_token, original.identity)
    assert UserRepo(product).count() == 1


async def test_company_selection_does_not_rebind_or_allocate_original_home_actor(
    sessions: _Environment, product: SqlitePool
) -> None:
    pair = await _login(sessions)
    resolver = _resolver(sessions, product)
    original = await authenticate(resolver, pair.access_token)
    company = await sessions.service.switch_organization(pair.access_token, _IDS["companyOrg"])
    with pytest.raises(MossworkError):
        await reauthenticate(resolver, company.access_token, original.identity.projection)
    assert UserRepo(product).count() == 1
    assert (await original.revalidate()).projection == original.identity.projection


@pytest.mark.parametrize(
    "field", ["authority_id", "account_id", "organization_id", "member_id", "local_user_id"]
)
async def test_existing_actor_constraint_is_exact_and_never_creates_a_mapping(
    sessions: _Environment, product: SqlitePool, field: str
) -> None:
    pair = await _login(sessions)
    resolver = _resolver(sessions, product)
    original = await authenticate(resolver, pair.access_token)
    projection = original.identity.projection
    if field == "local_user_id":
        projection = replace(projection, local_user_id=projection.local_user_id + 1)
    else:
        projection = replace(
            projection, identity=replace(projection.identity, **{field: "other-original-value"})
        )
    with pytest.raises(MossworkError):
        await reauthenticate(resolver, pair.access_token, projection)
    assert UserRepo(product).count() == 1


async def test_missing_existing_projection_cannot_allocate_or_guess_by_local_integer(
    sessions: _Environment, product: SqlitePool
) -> None:
    pair = await _login(sessions)
    resolver = _resolver(sessions, product)
    snapshot = await sessions.service.snapshots.snapshot(pair.access_token)
    expected = CanonicalLocalUserProjection(
        CanonicalIdentityKey(
            snapshot.authority_id,
            snapshot.account_id,
            snapshot.organization_id,
            snapshot.member.owner_id,
        ),
        1,
    )
    with pytest.raises(MossworkError):
        await reauthenticate(resolver, pair.access_token, expected)
    assert UserRepo(product).count() == 0


@pytest.mark.parametrize("change", ["membership", "product", "logout"])
async def test_changes_during_existing_projection_wait_refuse_new_principal(
    sessions: _Environment, product: SqlitePool, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    pair = await _login(sessions)
    resolver = _resolver(sessions, product)
    original = await authenticate(resolver, pair.access_token)
    previous = resolver._product
    calls = 0

    async def waited(key: CanonicalIdentityKey, *, allocate: bool) -> CanonicalLocalUserProjection:
        nonlocal calls
        assert not allocate
        result = await previous(key, allocate=allocate)
        calls += 1
        if calls == 1:
            if change == "membership":
                _write(
                    sessions,
                    "UPDATE users SET membership_revision=membership_revision+1 WHERE id=?",
                    (_IDS["homeMember"],),
                )
            elif change == "product":
                with product.transaction() as conn:
                    conn.execute("UPDATE users SET disabled=1 WHERE id=?", (result.local_user_id,))
            else:
                await sessions.service.logout(pair.access_token, pair.refresh_token)
        return result

    monkeypatch.setattr(resolver, "_product", waited)
    with pytest.raises(MossworkError):
        await reauthenticate(resolver, pair.access_token, original.identity.projection)
    assert UserRepo(product).count() == 1


async def test_refresh_and_decoded_metadata_cannot_authenticate_original_actor(
    sessions: _Environment, product: SqlitePool
) -> None:
    pair = await _login(sessions)
    resolver = _resolver(sessions, product)
    original = await authenticate(resolver, pair.access_token)
    with pytest.raises(MossworkError):
        await reauthenticate(resolver, pair.refresh_token, original.identity.projection)
    with pytest.raises(MossworkError):
        await reauthenticate(resolver, "not-a-token", original.identity.projection)
    with pytest.raises(MossworkError):
        await reauthenticate(resolver, pair.access_token, original.identity)  # type: ignore[arg-type]
    assert UserRepo(product).count() == 1
