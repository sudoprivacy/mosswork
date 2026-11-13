"""Retained TS/SQLite invitation catalogue and current administrator fences."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from tests.support.app import mosswork_client, write_mosswork_config
from tests.unit.identity.test_canonical_organization_members_port import _signed
from tests.unit.identity.test_canonical_organization_members_port import legacy_db as legacy_db
from tests.unit.identity.test_canonical_organization_members_port import members as members
from tests.unit.identity.test_canonical_organization_members_port import sessions as sessions
from tests.unit.identity.test_human_sessions import _IDS, _Environment, _login, _write
from tests.unit.identity.test_organization_directory import _seed_directory

from octop.api.app import build_app
from octop.api.deps import is_canonical_identity_request
from octop.infra.errors import MossworkError
from octop.infra.identity.organization_members import CanonicalOrganizationMemberService

_ORACLE = json.loads(
    (Path(__file__).parent / "fixtures/invitation_directory_legacy.json").read_text(
        encoding="utf-8"
    )
)


def _seed(environment: _Environment, role: str = "admin") -> None:
    _seed_directory(environment, "admin")
    _write(environment, "UPDATE users SET role=? WHERE id=?", (role, _IDS["homeMember"]))
    with closing(sqlite3.connect(environment.path)) as db, db:
        db.executescript(_ORACLE["schema"])
        for row in _ORACLE["rows"]:
            values = list(row)
            values[1] = _IDS["homeOrg"] if row[1] == "home" else _IDS["companyOrg"]
            db.execute("INSERT INTO invitations VALUES (?,?,?,?,?,?,?,?,?)", values)


@pytest.mark.parametrize("case", _ORACLE["cases"], ids=lambda case: case["name"])
async def test_original_scoped_sql_filters_pagination_and_admin_roles(
    sessions: _Environment, members: CanonicalOrganizationMemberService, case: dict[str, Any]
) -> None:
    _seed(sessions, case["role"])
    token = _signed(await _login(sessions))
    before = sessions.path.read_bytes()
    writer_calls = sessions.writer.calls
    args = {key: value for key, value in case["input"].items() if key != "orgId"}
    if case["status"] != 200:
        with pytest.raises(MossworkError) as refused:
            await members.list_invitations(token, **args)
        assert refused.value.status == case["status"]
    else:
        result = await members.list_invitations(token, **args)
        payload = asdict(result)
        payload["items"] = list(payload["items"])
        expected = json.loads(json.dumps(case["expected"]))
        for item in expected["items"]:
            item["organization_id"] = _IDS["homeOrg"]
        assert payload == expected
        assert all("FOREIGN" not in item.code for item in result.items)
        assert all(item.code not in repr(item) for item in result.items)
    assert sessions.path.read_bytes() == before
    assert sessions.writer.calls == writer_calls


@pytest.mark.parametrize("scopes", [[], ["sessions:list"]])
async def test_signed_administrator_scope_is_required(
    sessions: _Environment, members: CanonicalOrganizationMemberService, scopes: list[str]
) -> None:
    _seed(sessions)
    token = _signed(await _login(sessions), scopes=scopes)
    with pytest.raises(MossworkError) as refused:
        await members.list_invitations(token)
    assert refused.value.status == 403


@pytest.mark.parametrize("stage", ["before_read", "after_read", "final_check"])
@pytest.mark.parametrize("change", ["member_disabled", "account_disabled", "revision", "revoked"])
async def test_current_grant_loss_around_waits_refuses_captured_codes(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    change: str,
) -> None:
    _seed(sessions)
    login = await _login(sessions)
    token = _signed(login)
    from tests.unit.identity.test_human_sessions import _claims

    def revoke() -> None:
        if change == "member_disabled":
            _write(sessions, "UPDATE users SET status='disabled' WHERE id=?", (_IDS["homeMember"],))
        elif change == "account_disabled":
            _write(
                sessions,
                "UPDATE human_accounts SET status='disabled' WHERE id=?",
                (_IDS["account"],),
            )
        elif change == "revision":
            _write(
                sessions,
                "UPDATE users SET membership_revision=membership_revision+1 WHERE id=?",
                (_IDS["homeMember"],),
            )
        else:
            _write(
                sessions,
                "INSERT INTO revoked_tokens (jti,expires_at) VALUES (?,?)",
                (_claims(token)["jti"], int(sessions.now[0]) + 900),
            )

    if stage == "final_check":
        original_check = members._verify_invitation_access

        def check(snapshot: Any) -> None:
            revoke()
            original_check(snapshot)

        monkeypatch.setattr(members, "_verify_invitation_access", check)
    else:
        original_read = members._read_invitation_page

        def read(*args: Any):
            if stage == "before_read":
                revoke()
            result = original_read(*args)
            if stage == "after_read":
                revoke()
            return result

        monkeypatch.setattr(members, "_read_invitation_page", read)
    with pytest.raises(MossworkError) as refused:
        await members.list_invitations(token)
    assert refused.value.status in (401, 403)
    assert "CODE-" not in str(refused.value)


@pytest.mark.parametrize(
    "args",
    [{"status": "invalid"}, {"limit": True}, {"offset": False}, {"limit": 1.5}, {"offset": "1"}],
)
async def test_malformed_filter_does_not_reach_the_store(
    sessions: _Environment,
    members: CanonicalOrganizationMemberService,
    monkeypatch: pytest.MonkeyPatch,
    args: dict[str, Any],
) -> None:
    _seed(sessions)
    token = _signed(await _login(sessions))

    def unexpected(*_args: Any) -> None:
        raise AssertionError("Malformed selection reached invitation SQL")

    monkeypatch.setattr(members, "_read_invitation_page", unexpected)
    with pytest.raises(MossworkError) as refused:
        await members.list_invitations(token, **args)
    assert refused.value.status == 400


async def test_missing_original_table_refuses_without_initializing_it(
    sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed_directory(sessions, "admin")
    token = _signed(await _login(sessions))
    _write(sessions, "DROP TABLE invitations")
    before = sessions.path.read_bytes()
    with pytest.raises(MossworkError) as refused:
        await members.list_invitations(token)
    assert refused.value.status == 503 and sessions.path.read_bytes() == before


async def test_actual_http_route_schema_and_no_store(
    tmp_path: Path, sessions: _Environment, members: CanonicalOrganizationMemberService
) -> None:
    _seed(sessions)
    token = _signed(await _login(sessions))
    home = tmp_path / "product"
    write_mosswork_config(home, enable_api_docs=True, enable_dashboard=False)
    async with mosswork_client(home) as (_, server):
        app = build_app(server, canonical_sessions=sessions.service, canonical_members=members)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(
                "/api/identity/invitations?status=pending&limit=1&offset=0&organization_id=foreign",
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert response.json()["total"] == 2
            assert response.json()["items"][0]["code"] == "CODE-A"
            assert "FOREIGN" not in response.text
            fractional = await client.get(
                "/api/identity/invitations?status=revoked",
                headers={"Authorization": "Bearer " + token},
            )
            assert fractional.json()["items"][0]["initial_credit_units"] == 10.5
            denied = await client.get("/api/identity/invitations")
            assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
            invalid = await client.get(
                "/api/identity/invitations?status=invalid",
                headers={"Authorization": "Bearer " + token},
            )
            assert invalid.status_code == 422 and invalid.headers["cache-control"] == "no-store"
            assert (await client.get("/api/docs")).status_code == 200
            route = app.openapi()["paths"]["/api/identity/invitations"]["get"]
            assert "selected organization's" in route["summary"]
            assert route["responses"]["200"]["content"]["application/json"]["schema"][
                "$ref"
            ].endswith("OrganizationInvitationsResponse")


@pytest.mark.parametrize(
    ("method", "path", "injected", "expected"),
    [
        ("GET", "/api/identity/invitations", True, True),
        ("GET", "/api/identity/invitations", False, False),
        ("POST", "/api/identity/invitations", True, False),
        ("HEAD", "/api/identity/invitations", True, False),
        ("GET", "/api/identity/invitations/foreign", True, False),
        ("GET", "/api/identity/invitations-extra", True, False),
    ],
)
def test_exact_method_path_and_actual_injected_service_are_required(
    members: CanonicalOrganizationMemberService,
    method: str,
    path: str,
    injected: bool,
    expected: bool,
) -> None:
    app = FastAPI()
    app.state.canonical_members = members if injected else None
    request = Request({"type": "http", "method": method, "path": path, "headers": [], "app": app})
    assert is_canonical_identity_request(request) is expected
