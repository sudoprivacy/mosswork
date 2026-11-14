"""Original TS CRUD oracle and actual canonical SQLite grant/write races."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast
from uuid import UUID

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
from octop.infra.identity import organization_members
from octop.infra.identity.organization_members import (
    CanonicalOrganizationMemberService,
    DepartmentUpdate,
)

_ORACLE = json.loads(
    (Path(__file__).parent / "fixtures/department_mutations_legacy.json").read_text(
        encoding="utf-8"
    )
)


@pytest.fixture
def department_service(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> CanonicalOrganizationMemberService:
    with sqlite3.connect(sessions.path) as db:
        # Exact retained column/index additions; fixtures only, never product initialization.
        db.executescript(_ORACLE["supplementalSchema"])
    return members


def _seed(environment: _Environment, name: str = "create_admin_child") -> dict[str, Any]:
    case = next(case for case in _ORACLE["cases"] if case["name"] == name)
    with sqlite3.connect(environment.path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        for row in case["departments"]:
            db.execute(
                """INSERT INTO departments
                (id,org_id,parent_id,name,ext_dept_id,created_at,updated_at)
                VALUES (?,?,NULL,?,?,?,?)""",
                (
                    row["id"],
                    row["org_id"],
                    row["name"],
                    row["ext_dept_id"],
                    row["created_at"],
                    row["updated_at"],
                ),
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
                    """INSERT INTO users(id,org_id,email,name,role,status,department_id,created_at)
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


def _departments(environment: _Environment) -> list[dict[str, Any]]:
    with sqlite3.connect(environment.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM departments ORDER BY created_at,id")]


def _other_tables(environment: _Environment) -> dict[str, list[tuple[Any, ...]]]:
    with sqlite3.connect(environment.path) as db:
        tables = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] != "departments"
        ]
        return {
            table: db.execute(
                'SELECT * FROM "' + table.replace('"', '""') + '" ORDER BY rowid'
            ).fetchall()
            for table in tables
        }


def _changes(values: dict[str, Any]) -> DepartmentUpdate:
    names = {"parentId": "parent_id", "extDeptId": "external_id"}
    return cast(DepartmentUpdate, {names.get(key, key): value for key, value in values.items()})


async def _invoke(service: CanonicalOrganizationMemberService, token: str, action: str) -> Any:
    if action == "create":
        return await service.create_department(token, "New", parent_id="child")
    if action == "update":
        return await service.update_department(token, "empty", {"name": "Renamed"})
    return await service.delete_department(token, "empty")


@pytest.mark.parametrize("name", [case["name"] for case in _ORACLE["cases"]])
async def test_actual_canonical_department_mutations_match_original_SQL_oracle(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    case = _seed(sessions, name)
    token = (await _login(sessions)).access_token
    before = _other_tables(sessions)
    monkeypatch.setattr(organization_members, "uuid4", lambda: UUID(_ORACLE["createdId"]))
    changes = _changes(case["changes"])
    try:
        if case["action"] == "create":
            result = await department_service.create_department(token, **changes)
        elif case["action"] == "update":
            result = await department_service.update_department(
                token, case["departmentId"], changes
            )
        else:
            result = await department_service.delete_department(token, case["departmentId"])
    except MossworkError as error:
        assert case["error"] is not None
        assert error.status == case["error"]["status"]
    else:
        assert case["error"] is None
        assert ({"ok": True} if result is None else asdict(result)) == case["result"]
    assert _departments(sessions) == case["after"]
    assert _other_tables(sessions) == before


@pytest.mark.parametrize("action", ["create", "update", "delete"])
@pytest.mark.parametrize("scopes", [[], ["sessions:list"]])
async def test_current_department_role_also_requires_signed_scope(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
    action: str,
    scopes: list[str],
) -> None:
    _seed(sessions)
    token = _signed(await _login(sessions), scopes=scopes)
    before = _departments(sessions)
    with pytest.raises(MossworkError) as denied:
        await _invoke(department_service, token, action)
    assert denied.value.status == 403
    assert _departments(sessions) == before


@pytest.mark.parametrize("action", ["create", "update", "delete"])
@pytest.mark.parametrize("change", ["revoke", "disable", "revision", "expiry", "actor_department"])
async def test_mutation_authorization_rechecked_inside_transaction_after_writer_wait(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
    action: str,
    change: str,
) -> None:
    _seed(sessions, "create_dept_child")
    token = (await _login(sessions)).access_token
    claims, before = _claims(token), _departments(sessions)
    sessions.writer.calls = 0

    def mutate(call: int) -> None:
        if call != 1:
            return
        if change == "revoke":
            _write(
                sessions, "INSERT INTO revoked_tokens VALUES (?,?)", (claims["jti"], claims["exp"])
            )
        elif change == "disable":
            _write(sessions, "UPDATE users SET status='disabled' WHERE id=?", (_IDS["homeMember"],))
        elif change == "revision":
            _write(
                sessions, "UPDATE users SET membership_revision=1 WHERE id=?", (_IDS["homeMember"],)
            )
        elif change == "expiry":
            sessions.now[0] += 301
        else:
            _write(
                sessions,
                "UPDATE users SET department_id='sibling' WHERE id=?",
                (_IDS["homeMember"],),
            )

    sessions.writer.on_call = mutate
    with pytest.raises(MossworkError) as denied:
        await _invoke(department_service, token, action)
    assert denied.value.status == (403 if change == "actor_department" else 401)
    assert _departments(sessions) == before


@pytest.mark.parametrize("action", ["update", "delete"])
async def test_target_leaving_managed_subtree_during_writer_wait_refuses(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
    action: str,
) -> None:
    _seed(sessions, "create_dept_child")
    token = (await _login(sessions)).access_token
    sessions.writer.calls = 0
    sessions.writer.on_call = lambda call: (
        _write(sessions, "UPDATE departments SET parent_id='sibling' WHERE id='empty'")
        if call == 1
        else None
    )
    with pytest.raises(MossworkError) as denied:
        await _invoke(department_service, token, action)
    assert denied.value.status == 403
    assert next(row for row in _departments(sessions) if row["id"] == "empty")["name"] == "Empty"


@pytest.mark.parametrize("action", ["create", "update", "delete"])
async def test_missing_writer_never_mutates_retained_departments(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
    action: str,
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    before = _departments(sessions)
    sessions.writer.is_allowed = False
    with pytest.raises(MossworkError) as denied:
        await _invoke(department_service, token, action)
    assert denied.value.status == 503
    assert _departments(sessions) == before


@pytest.mark.parametrize("after_commit", ["writer_loss", "revocation"])
async def test_post_commit_refusal_preserves_committed_identity_without_retry(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
    after_commit: str,
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    claims = _claims(token)
    sessions.writer.calls = 0

    def mutate(call: int) -> None:
        if call == 2:
            if after_commit == "writer_loss":
                sessions.writer.is_allowed = False
            else:
                _write(
                    sessions,
                    "INSERT INTO revoked_tokens VALUES (?,?)",
                    (claims["jti"], claims["exp"]),
                )

    sessions.writer.on_call = mutate
    with pytest.raises(MossworkError) as denied:
        await department_service.create_department(token, "Committed", parent_id="child")
    assert denied.value.status == (503 if after_commit == "writer_loss" else 401)
    created = [row for row in _departments(sessions) if row["name"] == "Committed"]
    assert len(created) == 1 and UUID(created[0]["id"])


async def test_two_services_serialize_sibling_uniqueness_in_original_store(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    other = CanonicalOrganizationMemberService(
        sessions.path,
        sessions.service.snapshots,
        writer=sessions.writer,
        clock=lambda: sessions.now[0],
    )
    results = await asyncio.gather(
        department_service.create_department(token, "Racing", parent_id="child"),
        other.create_department(token, "Racing", parent_id="child"),
        return_exceptions=True,
    )
    assert len([result for result in results if not isinstance(result, BaseException)]) == 1
    assert [result.status for result in results if isinstance(result, MossworkError)] == [409]
    assert len([row for row in _departments(sessions) if row["name"] == "Racing"]) == 1


async def test_patch_intent_is_captured_before_any_authority_await(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    changes: DepartmentUpdate = {"name": "Selected"}
    sessions.writer.on_call = lambda _: changes.update(name="Later")
    result = await department_service.update_department(token, "empty", changes)
    assert result.name == "Selected"


async def test_inconsistent_retained_cycle_refuses_move_without_writing(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    _write(sessions, "UPDATE departments SET parent_id='grand' WHERE id='child'")
    before = _departments(sessions)
    with pytest.raises(MossworkError) as denied:
        await department_service.update_department(token, "empty", {"parent_id": "child"})
    assert denied.value.status == 400
    assert _departments(sessions) == before


async def test_existing_limits_and_other_identity_tables_are_preserved(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    _write(sessions, "ALTER TABLE departments ADD COLUMN token_limit INTEGER")
    _write(sessions, "UPDATE departments SET token_limit=17 WHERE id='empty'")
    before = _other_tables(sessions)
    await department_service.update_department(token, "empty", {"name": "Renamed"})
    assert next(row for row in _departments(sessions) if row["id"] == "empty")["token_limit"] == 17
    assert _other_tables(sessions) == before


async def test_missing_original_department_column_is_unavailable_without_schema_repair(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    _write(sessions, "DROP INDEX departments_ext_uniq")
    _write(sessions, "ALTER TABLE departments DROP COLUMN ext_dept_id")
    before = sessions.path.read_bytes()
    with pytest.raises(MossworkError) as denied:
        await department_service.create_department(token, "New")
    assert denied.value.status == 503
    assert sessions.path.read_bytes() == before


@pytest.mark.parametrize("constraint", ["missing", "nonunique", "partial_scope"])
async def test_missing_original_unique_index_refuses_without_recreating_it(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
    constraint: str,
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    _write(sessions, "DROP INDEX departments_ext_uniq")
    if constraint == "nonunique":
        _write(
            sessions,
            "CREATE INDEX departments_ext_uniq ON departments(org_id,ext_dept_id) "
            "WHERE ext_dept_id IS NOT NULL",
        )
    elif constraint == "partial_scope":
        _write(
            sessions,
            "CREATE UNIQUE INDEX departments_ext_uniq ON departments(org_id,ext_dept_id) "
            "WHERE org_id='different-fixture-organization' AND ext_dept_id IS NOT NULL",
        )
    before = sessions.path.read_bytes()
    with pytest.raises(MossworkError) as denied:
        await department_service.create_department(token, "New", external_id="external-root")
    assert denied.value.status == 503
    assert sessions.path.read_bytes() == before


async def test_actual_HTTP_department_mutations_use_only_injected_current_organization(
    sessions: _Environment,
    department_service: CanonicalOrganizationMemberService,
    tmp_path: Path,
) -> None:
    _seed(sessions)
    token = (await _login(sessions)).access_token
    home = tmp_path / "product"
    write_mosswork_config(home, enable_api_docs=True, enable_dashboard=False)
    async with mosswork_client(home) as (_, server):
        app = build_app(
            server, canonical_sessions=sessions.service, canonical_members=department_service
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            path, headers = "/api/identity/departments", {"Authorization": "Bearer " + token}
            assert (await client.post(path, json={"name": "New"})).status_code == 401
            assert (
                await client.post(
                    path,
                    json={"name": "New", "organization_id": _IDS["companyOrg"]},
                    headers=headers,
                )
            ).status_code == 422
            created = await client.post(
                path,
                json={"name": "New", "parent_id": "child", "external_id": "external-new"},
                headers=headers,
            )
            assert created.status_code == 200 and created.headers["cache-control"] == "no-store"
            value = created.json()["department"]
            assert value["organization_id"] == _IDS["homeOrg"]
            resource = path + "/" + value["department_id"]
            assert (
                await client.patch(resource, json={"name": "Changed"}, headers=headers)
            ).status_code == 200
            assert (
                await client.patch(resource, json={"name": None}, headers=headers)
            ).status_code == 400
            assert (await client.get(resource, headers=headers)).status_code == 503
            assert (
                await client.patch(resource + "/extra", json={"name": "Changed"}, headers=headers)
            ).status_code == 503
            deleted = await client.delete(resource, headers=headers)
            assert deleted.status_code == 200 and deleted.json() == {"ok": True}
            assert deleted.headers["cache-control"] == "no-store"
            for route, method in [
                (path, "post"),
                (path + "/{department_id}", "patch"),
                (path + "/{department_id}", "delete"),
            ]:
                operation = app.openapi()["paths"][route][method]
                assert operation["security"] == [{"CanonicalHumanBearer": []}]
                assert (
                    operation["summary"]
                    and operation["responses"]["200"]["content"]["application/json"]["schema"][
                        "$ref"
                    ]
                )
            assert (await client.get("/api/docs")).status_code == 200
            assert server.user_manager is not None and server.user_manager.count() == 0
