"""Actual canonical token/SQLite HTTP refusal and platform-scope boundaries."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from tests.unit.identity.test_canonical_organization_members_port import _signed
from tests.unit.identity.test_human_sessions import _IDS, _Environment, _login, _write
from tests.unit.identity.test_human_sessions import legacy_db as legacy_db
from tests.unit.identity.test_human_sessions import sessions as sessions
from tests.unit.identity.test_platform_login_policy import _Readiness, _row, _service, _super_token

from octop.api.routers.canonical_platform_policy import (
    build_canonical_platform_login_policy_router,
)

_URL = "/api/identity/platform/login-policy"


def _app(environment: _Environment, readiness: _Readiness | None = None) -> FastAPI:
    app = FastAPI()
    service = _service(environment, readiness)
    app.state.canonical_identity = environment.service.snapshots
    app.state.canonical_platform_login_policy = service
    app.include_router(
        build_canonical_platform_login_policy_router(service), prefix="/api/identity"
    )
    return app


@pytest.mark.parametrize(("method", "stored"), [("sms", 0), ("password", 1), ("cas", 2)])
async def test_explicit_platform_put_uses_current_actor_and_preserves_org_override(
    sessions: _Environment, method: str, stored: int
) -> None:
    token = await _super_token(sessions)
    with sqlite3.connect(sessions.path) as db:
        db.execute(
            "INSERT INTO client_delivery_policies VALUES ('organization',?,?,?,3,4)",
            (_IDS["homeOrg"], '{"loginMethod":0,"keep":"org"}', "original-org-writer"),
        )
    organization = _row(sessions, "organization", _IDS["homeOrg"])
    readiness = _Readiness()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, readiness)), base_url="http://test"
    ) as client:
        response = await client.put(
            _URL, json={"login_method": method}, headers={"Authorization": "Bearer " + token}
        )
    assert response.status_code == 200 and response.json() == {"success": True}
    assert response.headers["cache-control"] == "no-store"
    row = _row(sessions, "platform", "default")
    assert row is not None and json.loads(row[0])["loginMethod"] == stored
    assert row[1] == _IDS["homeMember"]
    assert _row(sessions, "organization", _IDS["homeOrg"]) == organization
    assert readiness.calls == ([method] if method != "password" else [])


@pytest.mark.parametrize(
    "body",
    [
        {"login_method": 1},
        {"login_method": True},
        {"login_method": "password", "organization_id": "foreign"},
        {"login_method": "password", "scope": "organization"},
        {"login_method": "password", "inherit_login_method": True},
        {"login_method": "password", "access_token": "private-body-marker"},
    ],
)
async def test_invalid_or_selector_body_is_redacted_before_writing(
    sessions: _Environment, body: dict[str, Any]
) -> None:
    token = await _super_token(sessions)
    calls = sessions.writer.calls
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions)), base_url="http://test"
    ) as client:
        response = await client.put(_URL, json=body, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 422 and "private-body-marker" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert sessions.writer.calls == calls and _row(sessions, "platform", "default") is None


async def test_missing_duplicate_and_query_credentials_refuse_without_writer(
    sessions: _Environment,
) -> None:
    token = await _super_token(sessions)
    calls = sessions.writer.calls
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions)), base_url="http://test"
    ) as client:
        for headers, suffix, status in [
            ([], "", 401),
            ([("Authorization", "Bearer " + token)] * 2, "", 401),
            ([("Authorization", "Bearer " + token)], "?scope=platform", 400),
        ]:
            response = await client.put(
                _URL + suffix, json={"login_method": "password"}, headers=headers
            )
            assert response.status_code == status
            assert response.headers["cache-control"] == "no-store"
    assert sessions.writer.calls == calls and _row(sessions, "platform", "default") is None


async def test_org_admin_is_not_promoted_by_explicit_platform_path(sessions: _Environment) -> None:
    _write(sessions, "UPDATE users SET role='admin' WHERE id=?", (_IDS["homeMember"],))
    token = (await _login(sessions)).access_token
    calls = sessions.writer.calls
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions)), base_url="http://test"
    ) as client:
        response = await client.put(
            _URL, json={"login_method": "password"}, headers={"Authorization": "Bearer " + token}
        )
    assert response.status_code == 403
    assert sessions.writer.calls == calls and _row(sessions, "platform", "default") is None


async def test_signed_ceiling_and_platform_readiness_still_apply(sessions: _Environment) -> None:
    _write(sessions, "UPDATE users SET role='super_admin' WHERE id=?", (_IDS["homeMember"],))
    tokens = await _login(sessions)
    limited = _signed(tokens, scopes=["admin:users"])
    readiness = _Readiness()
    readiness.result = False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, readiness)), base_url="http://test"
    ) as client:
        for token, method, status in [
            (limited, "password", 403),
            (tokens.access_token, "sms", 503),
        ]:
            response = await client.put(
                _URL, json={"login_method": method}, headers={"Authorization": "Bearer " + token}
            )
            assert response.status_code == status
    assert _row(sessions, "platform", "default") is None


@pytest.mark.parametrize("replacement", ["service", "snapshot"])
async def test_replaced_mounted_service_or_snapshot_refuses_before_write(
    sessions: _Environment, replacement: str
) -> None:
    token = await _super_token(sessions)
    app = _app(sessions)
    if replacement == "service":
        app.state.canonical_platform_login_policy = _service(sessions)
    else:
        app.state.canonical_identity = object()
    calls = sessions.writer.calls
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            _URL, json={"login_method": "password"}, headers={"Authorization": "Bearer " + token}
        )
    assert response.status_code == 503
    assert sessions.writer.calls == calls and _row(sessions, "platform", "default") is None


async def test_late_mount_withdrawal_refuses_ack_without_repeating_commit(
    sessions: _Environment,
) -> None:
    token = await _super_token(sessions)
    app = _app(sessions)
    before = sessions.writer.calls

    def withdraw(calls: int) -> None:
        if calls == before + 3:
            app.state.canonical_platform_login_policy = None

    sessions.writer.on_call = withdraw
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            _URL, json={"login_method": "password"}, headers={"Authorization": "Bearer " + token}
        )
    assert response.status_code == 503 and sessions.writer.calls == before + 3
    row = _row(sessions, "platform", "default")
    assert row is not None and json.loads(row[0])["loginMethod"] == 1


async def test_openapi_exposes_only_explicit_platform_put_without_inheritance(
    sessions: _Environment,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions)), base_url="http://test"
    ) as client:
        schema = (await client.get("/openapi.json")).json()
        response = await client.delete(_URL)
    assert response.status_code == 405
    assert set(schema["paths"][_URL]) == {"put"}
    assert schema["paths"][_URL]["put"]["summary"]
    body = schema["components"]["schemas"]["PlatformLoginMethodBody"]
    assert body["additionalProperties"] is False and set(body["properties"]) == {"login_method"}
    assert body["properties"]["login_method"]["enum"] == ["sms", "password", "cas"]
