"""Original admin password SQL parity and current canonical grant/write races."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.support.app import mosswork_client, write_mosswork_config
from tests.unit.identity.test_canonical_organization_members_port import _signed
from tests.unit.identity.test_canonical_organization_members_port import legacy_db as legacy_db
from tests.unit.identity.test_canonical_organization_members_port import members as members
from tests.unit.identity.test_canonical_organization_members_port import sessions as sessions
from tests.unit.identity.test_human_sessions import (
    _IDS,
    _claims,
    _Environment,
    _login,
    _set_policy,
    _write,
)
from tests.unit.identity.test_organization_directory import _seed_directory

from octop.api.app import build_app
from octop.api.routers.canonical_members import SetMemberPasswordBody
from octop.infra.errors import MossworkError
from octop.infra.identity.canonical import CanonicalSnapshotService, SqliteCanonicalSnapshotStore
from octop.infra.identity.organization_members import CanonicalOrganizationMemberService
from octop.infra.identity.password_auth import CanonicalPasswordAuthService, _verify_password

_ORACLE = json.loads(
    (Path(__file__).parent / "fixtures/member_password_legacy.json").read_text(encoding="utf-8")
)
_NEW = "PublicNew123"
_TARGET = "directory-user"


def _seed(environment: _Environment, name: str = "admin_user") -> dict[str, Any]:
    case = next(case for case in _ORACLE["cases"] if case["name"] == name)
    _seed_directory(environment, "admin")
    with closing(sqlite3.connect(environment.path)) as db, db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        for row in case["before"]:
            fields = [field for field in row if field in columns and field != "id"]
            db.execute(
                "UPDATE users SET " + ",".join(field + "=?" for field in fields) + " WHERE id=?",
                (*[row[field] for field in fields], row["id"]),
            )
    return case


def _all_rows(environment: _Environment) -> dict[str, list[dict[str, Any]]]:
    with closing(sqlite3.connect(environment.path)) as db:
        db.row_factory = sqlite3.Row
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {
            name: [dict(row) for row in db.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
            for name in tables
        }


def _user(environment: _Environment, member_id: str = _TARGET) -> dict[str, Any]:
    return next(row for row in _all_rows(environment)["users"] if row["id"] == member_id)


def test_fixture_pins_actual_original_service_scope_hash_and_sql() -> None:
    assert _ORACLE["sourceCommit"] == "d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d"
    assert _ORACLE["sourceHashes"] == {
        "src/server/auth/service.ts": "4670964f241ed457d3104c6b2b92abe020dea37b6cff393e9aab797309be71fa",
        "src/server/authCenter/db.ts": "f83b7ad772d28b54160315f96a7e3cd20648006cef4da5afd6082241c5571b0d",
    }
    assert len(_ORACLE["cases"]) == 22


@pytest.mark.parametrize("case", _ORACLE["cases"], ids=lambda case: case["name"])
async def test_original_admin_reset_scope_and_only_two_updated_columns(
    sessions: _Environment, members: CanonicalOrganizationMemberService, case: dict[str, Any]
) -> None:
    _seed(sessions, case["name"])
    token = (await _login(sessions)).access_token
    before = _all_rows(sessions)
    if case["status"] != 200:
        with pytest.raises(MossworkError) as refused:
            await members.set_member_password(token, case["memberId"], case["password"])
        assert refused.value.status == case["status"]
        assert _all_rows(sessions) == before
        assert case["before"] == case["after"]
        return
    await members.set_member_password(token, case["memberId"], case["password"])
    row = _user(sessions, case["memberId"])
    original = next(row for row in case["after"] if row["id"] == case["memberId"])
    assert _verify_password(case["password"], row["password_hash"], None) == (True, None)
    assert _verify_password(case["password"], original["password_hash"], None) == (True, None)
    assert row["password_updated_at"] == original["password_updated_at"] == _ORACLE["timestamp"]
    # Every retained table, including other memberships, limits, identities and
    # revocations, remains identical apart from the two original target columns.
    expected = before
    target = next(row for row in expected["users"] if row["id"] == case["memberId"])
    target.update(
        password_hash=row["password_hash"], password_updated_at=row["password_updated_at"]
    )
    assert _all_rows(sessions) == expected
    assert row["local_auth"] == row["local_execution_allowed"] == 0
    assert row["membership_revision"] == 7 and row["token_limit"] == 17
    await sessions.service.snapshots.snapshot(token)


@pytest.mark.parametrize("scopes", [[], ["sessions:list"]])
async def test_signed_scope_is_required_even_for_current_full_admin(
    sessions: _Environment, members: CanonicalOrganizationMemberService, scopes: list[str]
) -> None:
    _seed(sessions)
    token = _signed(await _login(sessions), scopes=scopes)
    before = _all_rows(sessions)
    with pytest.raises(MossworkError) as refused:
        await members.set_member_password(token, _TARGET, _NEW)
    assert refused.value.status == 403 and _all_rows(sessions) == before


async def test_plain_member_cannot_reset_with_a_broad_signed_ceiling(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions)
    _write(sessions, "UPDATE users SET role='user' WHERE id=?", (_IDS["homeMember"],))
    token = _signed(await _login(sessions))
    before = _all_rows(sessions)
    with pytest.raises(MossworkError) as refused:
        await members.set_member_password(token, _TARGET, _NEW)
    assert refused.value.status == 403 and _all_rows(sessions) == before


@pytest.mark.parametrize("boundary", ["hash", "writer"])
@pytest.mark.parametrize(
    "change,status",
    [
        ("revoked", 401),
        ("expiry", 401),
        ("account_disabled", 401),
        ("actor_disabled", 401),
        ("actor_revision", 401),
        ("actor_role", 401),
        ("actor_department", 403),
        ("target_department", 403),
        ("target_role", 403),
        ("tree", 403),
        ("target_org", 404),
        ("target_deleted", 404),
    ],
)
async def test_current_scope_rechecked_after_hash_and_writer_wait(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    change: str,
    status: int,
) -> None:
    _seed(sessions, "dept_user")
    token = (await _login(sessions)).access_token
    original_hash = _user(sessions)["password_hash"]

    def mutate() -> None:
        claims = _claims(token)
        if change == "revoked":
            _write(
                sessions, "INSERT INTO revoked_tokens VALUES (?,?)", (claims["jti"], claims["exp"])
            )
        elif change == "expiry":
            sessions.now[0] += 301
        elif change == "account_disabled":
            _write(sessions, "UPDATE human_accounts SET status='disabled'")
        elif change == "actor_disabled":
            _write(sessions, "UPDATE users SET status='disabled' WHERE id=?", (_IDS["homeMember"],))
        elif change == "actor_revision":
            _write(
                sessions, "UPDATE users SET membership_revision=8 WHERE id=?", (_IDS["homeMember"],)
            )
        elif change == "actor_role":
            _write(sessions, "UPDATE users SET role='user' WHERE id=?", (_IDS["homeMember"],))
        elif change == "actor_department":
            _write(
                sessions,
                "UPDATE users SET department_id='sibling' WHERE id=?",
                (_IDS["homeMember"],),
            )
        elif change == "target_department":
            _write(sessions, "UPDATE users SET department_id='sibling' WHERE id=?", (_TARGET,))
        elif change == "target_role":
            _write(sessions, "UPDATE users SET role='admin' WHERE id=?", (_TARGET,))
        elif change == "tree":
            _write(sessions, "UPDATE departments SET parent_id='sibling' WHERE id='child'")
            _write(
                sessions, "UPDATE users SET department_id='root' WHERE id=?", (_IDS["homeMember"],)
            )
            # Root still contains sibling, so move the selected child outside it.
            _write(sessions, "UPDATE departments SET parent_id=NULL WHERE id='child'")
        elif change == "target_org":
            _write(sessions, "UPDATE users SET org_id=? WHERE id=?", (_IDS["companyOrg"], _TARGET))
        else:
            _write(sessions, "DELETE FROM users WHERE id=?", (_TARGET,))

    if boundary == "writer":
        next_write = sessions.writer.calls + 2
        sessions.writer.on_call = lambda count: mutate() if count == next_write else None
    else:
        original = members._prepare_password_reset

        def prepare(*args: Any) -> str:
            result = original(*args)
            mutate()
            return result

        monkeypatch.setattr(members, "_prepare_password_reset", prepare)
    with pytest.raises(MossworkError) as refused:
        await members.set_member_password(token, _TARGET, _NEW)
    assert refused.value.status == status
    if change != "target_deleted":
        assert _user(sessions)["password_hash"] == original_hash


@pytest.mark.parametrize("writer_check,committed", [(1, False), (2, False), (3, True)])
async def test_writer_withdrawal_preserves_actual_commit_boundary(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    writer_check: int,
    committed: bool,
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    old_hash = _user(sessions)["password_hash"]
    refuse_at = sessions.writer.calls + writer_check

    def withdraw(count: int) -> None:
        if count == refuse_at:
            sessions.writer.is_allowed = False

    sessions.writer.on_call = withdraw
    with pytest.raises(MossworkError) as refused:
        await members.set_member_password(token, _TARGET, _NEW)
    assert refused.value.status == 503
    if committed:
        assert _verify_password(_NEW, _user(sessions)["password_hash"], None) == (True, None)
    else:
        assert _user(sessions)["password_hash"] == old_hash


async def test_revocation_after_commit_refuses_response_without_replay_or_fake_rollback(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    original = members._write_password_reset
    calls = 0

    def write_then_revoke(*args: Any) -> None:
        nonlocal calls
        calls += 1
        original(*args)
        claims = _claims(token)
        _write(sessions, "INSERT INTO revoked_tokens VALUES (?,?)", (claims["jti"], claims["exp"]))

    monkeypatch.setattr(members, "_write_password_reset", write_then_revoke)
    with pytest.raises(MossworkError) as refused:
        await members.set_member_password(token, _TARGET, _NEW)
    assert refused.value.status == 401 and calls == 1
    assert _verify_password(_NEW, _user(sessions)["password_hash"], None) == (True, None)


async def test_reset_preserves_actual_login_flags_old_tokens_refresh_and_other_membership(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions)
    actor = _IDS["homeMember"]
    passwords = CanonicalPasswordAuthService(
        sessions.path, sessions.policy, writer=sessions.writer, issuer=sessions.service
    )
    original = await passwords.login_moss(username="home", password="PublicOld123")
    company = _user(sessions, _IDS["companyMember"])
    sessions.now[0] += 30
    await members.set_member_password(original.access_token, actor, _NEW)
    with pytest.raises(MossworkError):
        await passwords.login_moss(username="home", password="PublicOld123")
    current = await passwords.login_moss(username="home", password=_NEW)
    assert current.account_id == original.account_id and current.member_id == actor
    await sessions.service.snapshots.snapshot(original.access_token)
    assert (await sessions.service.refresh(original.refresh_token)).member_id == actor
    assert _user(sessions, _IDS["companyMember"]) == company
    # Original password login does not inspect local_auth. The ordinary realm
    # alone requires the organization's current password login policy.
    with pytest.raises(MossworkError):
        await passwords.login_sudowork(username="home", password=_NEW)
    _set_policy(sessions, "password")
    assert (await passwords.login_sudowork(username="home", password=_NEW)).member_id == actor
    assert _user(sessions, actor)["local_auth"] == 0


async def test_missing_retained_store_is_not_created(
    sessions: _Environment, members: CanonicalOrganizationMemberService, tmp_path: Path
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    snapshot = await sessions.service.snapshots.snapshot(token)
    missing = tmp_path / "missing.sqlite"
    unavailable = CanonicalOrganizationMemberService(
        missing,
        CanonicalSnapshotService(
            SqliteCanonicalSnapshotStore(missing, authority_id=snapshot.authority_id),
            sessions.service.snapshots._verifier,
            clock=lambda: sessions.now[0],
        ),
        writer=sessions.writer,
        clock=lambda: sessions.now[0],
    )
    with pytest.raises(MossworkError):
        await unavailable.set_member_password(token, _TARGET, _NEW)
    assert not missing.exists()
    # Even the internal admitted write path opens only an existing store.
    with pytest.raises(MossworkError) as refused:
        unavailable._write_password_reset(snapshot, _TARGET, "fixture-never-written")
    assert refused.value.status == 503 and not missing.exists()


@pytest.mark.parametrize("verdict", [False, True, {"allowed": True}])
async def test_malformed_writer_completion_cannot_allow_password_effect(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    monkeypatch: pytest.MonkeyPatch,
    verdict: Any,
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    before = _all_rows(sessions)

    async def malformed() -> Any:
        return verdict

    monkeypatch.setattr(sessions.writer, "assert_current_writer", malformed)
    with pytest.raises(MossworkError) as refused:
        await members.set_member_password(token, _TARGET, _NEW)
    assert refused.value.status == 503 and _all_rows(sessions) == before


async def test_sql_failure_rolls_back_password_without_exporting_sensitive_driver_text(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    with closing(sqlite3.connect(sessions.path)) as db:
        db.executescript(
            "CREATE TRIGGER refuse_password BEFORE UPDATE OF password_hash ON users "
            "BEGIN SELECT RAISE(ABORT,'private-fixture-credential-driver-error'); END;"
        )
    before = _all_rows(sessions)
    with pytest.raises(MossworkError) as refused:
        await members.set_member_password(token, _TARGET, _NEW)
    assert refused.value.status == 503
    assert "private-fixture-credential-driver-error" not in repr(refused.value)
    assert _all_rows(sessions) == before


async def test_http_password_adapter_current_org_secret_redaction_and_openapi(
    sessions: _Environment, members: CanonicalOrganizationMemberService, tmp_path: Path
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    headers = {"Authorization": "Bearer " + token}
    home = tmp_path / "product"
    write_mosswork_config(home, enable_api_docs=True, enable_dashboard=False)
    assert _NEW not in repr(SetMemberPasswordBody(password=_NEW))
    async with mosswork_client(home) as (_, server):
        app = build_app(server, canonical_sessions=sessions.service, canonical_members=members)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            path = "/api/identity/members/" + _TARGET + "/password"
            assert (await client.post(path, json={"password": _NEW})).status_code == 401
            before = _all_rows(sessions)
            for body in (
                {},
                {"password": 123},
                {"password": _NEW, "organization_id": _IDS["companyOrg"]},
                {"password": _NEW, "nativeSessionId": "untrusted"},
            ):
                refused = await client.post(path, json=body, headers=headers)
                assert refused.status_code == 422 and _NEW not in refused.text
                assert _all_rows(sessions) == before
            empty = await client.post(path, json={"password": ""}, headers=headers)
            assert empty.status_code == 400 and _all_rows(sessions) == before
            result = await client.post(path, json={"password": _NEW}, headers=headers)
            assert result.status_code == 200 and result.json() == {"ok": True}
            assert result.headers["cache-control"] == "no-store" and _NEW not in result.text
            foreign = await client.post(
                "/api/identity/members/" + _IDS["companyMember"] + "/password",
                json={"password": _NEW},
                headers=headers,
            )
            assert foreign.status_code == 404
            assert (
                await client.put(path, json={"password": _NEW}, headers=headers)
            ).status_code == 503
            assert (await client.post(path + "/extra", headers=headers)).status_code == 503
            assert (await client.get("/api/docs")).status_code == 200
            operation = app.openapi()["paths"]["/api/identity/members/{member_id}/password"]["post"]
            assert operation["summary"] and operation["description"]
            assert operation["security"] == [{"CanonicalHumanBearer": []}]
            schemas = app.openapi()["components"]["schemas"]
            assert schemas["SetMemberPasswordBody"]["required"] == ["password"]
            assert schemas["MemberPasswordResponse"]["properties"]["ok"]["const"] is True
            assert server.user_manager is not None and server.user_manager.count() == 0
