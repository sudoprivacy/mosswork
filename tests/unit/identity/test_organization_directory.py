"""Original TS directory oracle, real canonical login and post-read scope races."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.support.app import mosswork_client, write_mosswork_config
from tests.unit.identity.test_canonical_organization_members_port import _signed
from tests.unit.identity.test_canonical_organization_members_port import legacy_db as legacy_db
from tests.unit.identity.test_canonical_organization_members_port import members as members
from tests.unit.identity.test_canonical_organization_members_port import sessions as sessions
from tests.unit.identity.test_human_sessions import _IDS, _claims, _Environment, _login, _write

from octop.api.app import build_app
from octop.infra.errors import MossworkError
from octop.infra.identity.organization_members import CanonicalOrganizationMemberService

_ORACLE = json.loads(
    (Path(__file__).parent / "fixtures/organization_directory_legacy.json").read_text(
        encoding="utf-8"
    )
)


def _seed_directory(environment: _Environment, name: str = "dept_admin") -> dict[str, Any]:
    case = next(case for case in _ORACLE["cases"] if case["name"] == name)
    with sqlite3.connect(environment.path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        # Original FK constraints stay active, including the retained cycle case.
        for row in case["departments"]:
            db.execute(
                "INSERT INTO departments VALUES (?,?,NULL,?,?,?)",
                (row["id"], row["org_id"], row["name"], row["created_at"], row["updated_at"]),
            )
        for row in case["departments"]:
            db.execute(
                "UPDATE departments SET parent_id=? WHERE id=?", (row["parent_id"], row["id"])
            )
        for row in case["users"]:
            if row["id"] == _IDS["homeMember"]:
                db.execute(
                    "UPDATE users SET role=?,department_id=? WHERE id=?",
                    (row["role"], row["department_id"], row["id"]),
                )
            else:
                db.execute(
                    """INSERT INTO users
                    (id,org_id,email,name,role,status,department_id,created_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        row["id"],
                        row["org_id"],
                        row["id"] + "@example.test",
                        row["name"],
                        row["role"],
                        row["status"],
                        row["department_id"],
                        row["created_at"],
                    ),
                )
    return case


@pytest.mark.parametrize("name", [case["name"] for case in _ORACLE["cases"]])
async def test_exact_original_directory_oracle_without_writer_or_schema_changes(
    sessions: _Environment, members: CanonicalOrganizationMemberService, name: str
) -> None:
    case = _seed_directory(sessions, name)
    token = (await _login(sessions)).access_token
    sessions.writer.is_allowed = False
    calls, before = sessions.writer.calls, sessions.path.read_bytes()
    users = await members.list_members(token)
    departments = await members.list_departments(token)
    assert [user.member_id for user in users] == case["expectedMembers"]
    assert [asdict(department) for department in departments] == case["expectedDepartments"]
    assert all(user.organization_id == _IDS["homeOrg"] for user in users)
    assert all(department.organization_id == _IDS["homeOrg"] for department in departments)
    if name not in ("admin", "super_admin"):
        assert all(user.role in ("user", "dept_admin") for user in users)
    assert sessions.writer.calls == calls
    assert sessions.path.read_bytes() == before
    assert _ORACLE["sourceCommit"] == "d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d"


@pytest.mark.parametrize("operation", ["members", "departments"])
@pytest.mark.parametrize("scopes", [[], ["sessions:list"]])
async def test_department_admin_needs_current_signed_administration_scope(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    operation: str,
    scopes: list[str],
) -> None:
    _seed_directory(sessions)
    token = _signed(await _login(sessions), scopes=scopes)
    method = members.list_members if operation == "members" else members.list_departments
    with pytest.raises(MossworkError) as denied:
        await method(token)
    assert denied.value.status == 403


async def test_ordinary_member_does_not_gain_directory_from_a_broad_signed_ceiling(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
) -> None:
    _seed_directory(sessions)
    _write(sessions, "UPDATE users SET role='user' WHERE id=?", (_IDS["homeMember"],))
    token = _signed(await _login(sessions))
    with pytest.raises(MossworkError) as denied:
        await members.list_departments(token)
    assert denied.value.status == 403


@pytest.mark.parametrize("change", ["actor_department", "target_department", "target_role", "tree"])
async def test_directory_scope_changes_during_read_refuse_the_original_result(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    _seed_directory(sessions)
    token = (await _login(sessions)).access_token
    original = members._list

    def read_then_change(snapshot: Any):
        result = original(snapshot)
        if change == "actor_department":
            _write(
                sessions,
                "UPDATE users SET department_id='sibling' WHERE id=?",
                (_IDS["homeMember"],),
            )
        elif change == "target_department":
            _write(sessions, "UPDATE users SET department_id='sibling' WHERE id='directory-user'")
        elif change == "target_role":
            _write(sessions, "UPDATE users SET role='admin' WHERE id='directory-user'")
        else:
            _write(sessions, "UPDATE departments SET parent_id='sibling' WHERE id='grand'")
        return result

    monkeypatch.setattr(members, "_list", read_then_change)
    with pytest.raises(MossworkError) as denied:
        await members.list_members(token)
    assert denied.value.status == 403


@pytest.mark.parametrize("change", ["revoke", "disable", "demote", "revision", "expiry", "tree"])
async def test_department_response_rechecks_human_and_tree_after_io(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    _seed_directory(sessions)
    token = (await _login(sessions)).access_token
    claims, original, calls = _claims(token), members._directory, 0

    def read_then_change(snapshot: Any):
        nonlocal calls
        result = original(snapshot)
        calls += 1
        if calls != 1:
            return result
        if change == "revoke":
            _write(
                sessions, "INSERT INTO revoked_tokens VALUES (?,?)", (claims["jti"], claims["exp"])
            )
        elif change == "disable":
            _write(sessions, "UPDATE users SET status='disabled' WHERE id=?", (_IDS["homeMember"],))
        elif change == "demote":
            _write(sessions, "UPDATE users SET role='user' WHERE id=?", (_IDS["homeMember"],))
        elif change == "revision":
            _write(
                sessions, "UPDATE users SET membership_revision=1 WHERE id=?", (_IDS["homeMember"],)
            )
        elif change == "expiry":
            sessions.now[0] += 301
        else:
            _write(sessions, "UPDATE departments SET parent_id='sibling' WHERE id='grand'")
        return result

    monkeypatch.setattr(members, "_directory", read_then_change)
    with pytest.raises(MossworkError) as denied:
        await members.list_departments(token)
    assert denied.value.status == (403 if change == "tree" else 401)


async def test_dept_admin_directory_does_not_grant_membership_writes(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
) -> None:
    _seed_directory(sessions)
    token = (await _login(sessions)).access_token
    calls = sessions.writer.calls
    with pytest.raises(MossworkError) as denied:
        await members.add_registered_account(token, "13900000002")
    assert denied.value.status == 403
    assert sessions.writer.calls == calls


async def test_moss_entitlement_change_does_not_revoke_org_directory_administration(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_directory(sessions, "admin")
    token = (await _login(sessions)).access_token
    original, calls = members._directory, 0

    def read_then_change(snapshot: Any):
        nonlocal calls
        result = original(snapshot)
        calls += 1
        if calls == 1:
            _write(
                sessions,
                "INSERT INTO moss_member_access VALUES (?,?,0,?,1)",
                (_IDS["homeOrg"], _IDS["homeMember"], _IDS["homeMember"]),
            )
        return result

    monkeypatch.setattr(members, "_directory", read_then_change)
    result = await members.list_departments(token)
    assert {department.department_id for department in result} == {
        "root",
        "child",
        "grand",
        "sibling",
    }
    assert (await sessions.service.snapshots.snapshot(token)).personal_agent is None


async def test_actual_canonical_http_directory_and_openapi_keep_scope_and_projection_separate(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    tmp_path: Path,
) -> None:
    case = _seed_directory(sessions)
    token = (await _login(sessions)).access_token
    home = tmp_path / "product"
    write_mosswork_config(home, enable_api_docs=True, enable_dashboard=False)
    async with mosswork_client(home) as (_, server):
        unavailable = build_app(server, canonical_sessions=sessions.service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=unavailable), base_url="http://testserver"
        ) as client:
            assert (
                await client.get(
                    "/api/identity/departments", headers={"Authorization": "Bearer " + token}
                )
            ).status_code == 503
        app = build_app(server, canonical_sessions=sessions.service, canonical_members=members)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            path, headers = "/api/identity/departments", {"Authorization": "Bearer " + token}
            assert (await client.get(path)).status_code == 401
            result = await client.get(path, headers=headers)
            assert result.status_code == 200
            assert result.json() == {"departments": case["expectedDepartments"]}
            assert result.headers["cache-control"] == "no-store"
            assert (await client.post(path, headers=headers)).status_code == 422
            assert (await client.put(path, headers=headers)).status_code == 503
            assert (await client.get(path + "/extra", headers=headers)).status_code == 503
            users = (await client.get("/api/identity/members", headers=headers)).json()["members"]
            assert [user["member_id"] for user in users] == case["expectedMembers"]
            assert {user["department_id"] for user in users} <= {"child", "grand"}
            operation = app.openapi()["paths"][path]["get"]
            assert operation["security"] == [{"CanonicalHumanBearer": []}]
            assert (await client.get("/api/docs")).status_code == 200
            assert server.user_manager is not None and server.user_manager.count() == 0
