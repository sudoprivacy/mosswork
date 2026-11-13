"""Actual canonical-token/SQLite checks for the explicit policy HTTP adapter."""

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
from tests.unit.identity.test_organization_login_policy import (
    _policy,
    _Readiness,
    _service,
)

from octop.api.routers.canonical_login_policy import (
    build_canonical_organization_login_policy_router,
)

_URL = "/api/identity/organization/login-policy"


def _app(environment: _Environment, readiness: _Readiness | None = None) -> FastAPI:
    app = FastAPI()
    service = _service(environment, readiness or _Readiness())
    app.state.canonical_identity = environment.service.snapshots
    app.state.canonical_organization_login_policy = service
    app.include_router(
        build_canonical_organization_login_policy_router(service),
        prefix="/api/identity",
    )
    return app


async def _administrator(environment: _Environment) -> str:
    _write(environment, "UPDATE users SET role='admin' WHERE id=?", (_IDS["homeMember"],))
    token = (await _login(environment)).access_token
    assert isinstance(token, str)
    return token


@pytest.mark.parametrize(("method", "stored"), [("sms", 0), ("password", 1), ("cas", 2)])
async def test_put_uses_actual_selected_canonical_actor_and_original_policy(
    sessions: _Environment, method: str, stored: int
) -> None:
    token = await _administrator(sessions)
    readiness = _Readiness()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, readiness)),
        base_url="http://test",
    ) as client:
        result = await client.put(
            _URL,
            json={"login_method": method},
            headers={"Authorization": "Bearer " + token},
        )
    assert result.status_code == 200
    assert result.json() == {"success": True}
    assert result.headers["cache-control"] == "no-store"
    policy = _policy(sessions)
    assert policy is not None
    assert json.loads(policy[0])["loginMethod"] == stored
    assert policy[1] == _IDS["homeMember"]
    assert readiness.calls == ([(method, _IDS["homeOrg"])] if method != "password" else [])


async def test_delete_restores_only_selected_org_inheritance(
    sessions: _Environment,
) -> None:
    token = await _administrator(sessions)
    with sqlite3.connect(sessions.path) as db:
        db.execute(
            "INSERT INTO client_delivery_policies VALUES ('organization',?,?,?,?,?)",
            (
                _IDS["homeOrg"],
                '{"loginMethod":1,"loginMethodInherited":false,"keep":7}',
                "old",
                3,
                4,
            ),
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions)), base_url="http://test"
    ) as client:
        result = await client.delete(_URL, headers={"Authorization": "Bearer " + token})
    assert result.status_code == 200
    policy = _policy(sessions)
    assert policy is not None
    assert json.loads(policy[0]) == {"loginMethodInherited": True, "keep": 7}
    assert policy[1:3] == (_IDS["homeMember"], 3)


@pytest.mark.parametrize(
    "body",
    [
        {"login_method": 1},
        {"login_method": True},
        {"login_method": "password", "organization_id": "foreign-selector"},
        {"login_method": "password", "scope": "platform"},
        {"login_method": "password", "access_token": "private-body-marker"},
    ],
)
async def test_invalid_body_is_redacted_before_any_policy_write(
    sessions: _Environment, body: dict[str, Any]
) -> None:
    token = await _administrator(sessions)
    writer_calls = sessions.writer.calls
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions)), base_url="http://test"
    ) as client:
        result = await client.put(_URL, json=body, headers={"Authorization": "Bearer " + token})
    assert result.status_code == 422
    assert result.headers["cache-control"] == "no-store"
    assert "private-body-marker" not in result.text
    assert sessions.writer.calls == writer_calls
    assert _policy(sessions) is None


@pytest.mark.parametrize("verb", ["PUT", "DELETE"])
async def test_duplicate_or_missing_bearer_refuses_before_writer(
    sessions: _Environment, verb: str
) -> None:
    token = await _administrator(sessions)
    writer_calls = sessions.writer.calls
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions)), base_url="http://test"
    ) as client:
        for headers in [[], [("Authorization", "Bearer " + token)] * 2]:
            payload = {"login_method": "password"} if verb == "PUT" else None
            result = await client.request(verb, _URL, headers=headers, json=payload)
            assert result.status_code == 401
            assert result.headers["cache-control"] == "no-store"
    assert sessions.writer.calls == writer_calls
    assert _policy(sessions) is None


@pytest.mark.parametrize("verb", ["PUT", "DELETE"])
async def test_query_cannot_select_another_org_or_platform(
    sessions: _Environment, verb: str
) -> None:
    token = await _administrator(sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions)), base_url="http://test"
    ) as client:
        payload = {"login_method": "password"} if verb == "PUT" else None
        result = await client.request(
            verb,
            _URL + "?scope=platform",
            headers={"Authorization": "Bearer " + token},
            json=payload,
        )
    assert result.status_code == 400
    assert _policy(sessions) is None


async def test_delete_refuses_payload_instead_of_ignoring_selectors(
    sessions: _Environment,
) -> None:
    token = await _administrator(sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions)), base_url="http://test"
    ) as client:
        result = await client.request(
            "DELETE",
            _URL,
            json={"scope": "platform"},
            headers={"Authorization": "Bearer " + token},
        )
    assert result.status_code == 400
    assert _policy(sessions) is None


async def test_current_signed_ceiling_and_unavailable_readiness_propagate(
    sessions: _Environment,
) -> None:
    _write(sessions, "UPDATE users SET role='admin' WHERE id=?", (_IDS["homeMember"],))
    tokens = await _login(sessions)
    token, limited = tokens.access_token, _signed(tokens, scopes=["admin:users"])
    readiness = _Readiness()
    readiness.result = False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, readiness)),
        base_url="http://test",
    ) as client:
        for current_token, method, status in [
            (limited, "password", 403),
            (token, "sms", 503),
        ]:
            result = await client.put(
                _URL,
                json={"login_method": method},
                headers={"Authorization": "Bearer " + current_token},
            )
            assert result.status_code == status
            assert result.headers["cache-control"] == "no-store"
    assert _policy(sessions) is None


@pytest.mark.parametrize("replacement", ["service", "snapshot"])
async def test_replaced_bound_service_or_snapshot_refuses_before_writer(
    sessions: _Environment, replacement: str
) -> None:
    token = await _administrator(sessions)
    app = _app(sessions)
    if replacement == "service":
        # A second actual service shares snapshots but has different ports.
        app.state.canonical_organization_login_policy = _service(sessions, _Readiness())
    else:
        app.state.canonical_identity = object()
    calls = sessions.writer.calls
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.put(
            _URL, json={"login_method": "password"}, headers={"Authorization": "Bearer " + token}
        )
    assert result.status_code == 503
    assert result.headers["cache-control"] == "no-store"
    assert sessions.writer.calls == calls
    assert _policy(sessions) is None


async def test_withdrawn_binding_after_commit_refuses_ack_without_retry(
    sessions: _Environment,
) -> None:
    token = await _administrator(sessions)
    app = _app(sessions)
    before = sessions.writer.calls

    def withdraw(calls: int) -> None:
        if calls == before + 3:
            app.state.canonical_organization_login_policy = None

    sessions.writer.on_call = withdraw
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.put(
            _URL, json={"login_method": "password"}, headers={"Authorization": "Bearer " + token}
        )
    assert result.status_code == 503
    assert sessions.writer.calls == before + 3
    policy = _policy(sessions)
    assert policy is not None
    assert json.loads(policy[0])["loginMethod"] == 1
    assert policy[1] == _IDS["homeMember"]


async def test_openapi_has_readable_summaries_and_strict_policy_schema(
    sessions: _Environment,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions)), base_url="http://test"
    ) as client:
        response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    methods = schema["paths"][_URL]
    assert set(methods) == {"put", "delete"}
    for operation in methods.values():
        assert operation["summary"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"]
    body = schema["components"]["schemas"]["OrganizationLoginMethodBody"]
    assert body["additionalProperties"] is False
    assert set(body["properties"]) == {"login_method"}
    assert body["properties"]["login_method"]["enum"] == ["sms", "password", "cas"]
