"""Real canonical token/SQLite HTTP with fixture vault and no shared mount edit."""

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
from tests.unit.identity.test_sms_configuration_updates import _prepared, _rows

from octop.api.routers.canonical_sms_configuration import (
    ManagedSmsConfigurationBody,
    build_canonical_sms_configuration_router,
)
from octop.infra.identity.sms_configuration_updates import CanonicalManagedSmsConfigurationService

_URL = "/api/identity/platform/sms-configuration"
_BODY: dict[str, Any] = {"expected_version": "original-version", "configuration": {}}


def _app(environment: _Environment, service: CanonicalManagedSmsConfigurationService) -> FastAPI:
    app = FastAPI()
    app.state.canonical_identity = environment.service.snapshots
    app.state.canonical_sms_configuration = service
    app.include_router(build_canonical_sms_configuration_router(service), prefix="/api/identity")
    return app


async def test_get_only_projects_original_metadata_without_vault_or_writer_access(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    sessions.writer.is_allowed = False
    calls, original = sessions.writer.calls, sessions.path.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, service)), base_url="http://test"
    ) as client:
        response = await client.get(_URL, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == {
        "managed": True,
        "saved_version": "original-version",
        "configuration": {
            "enabled": True,
            "mockDelivery": False,
            "sdkAppId": "fixture-app",
            "signName": "fixture-sign",
            "templateId": "fixture-template",
            "region": "ap-beijing",
            "templateParams": ["{code}", "{ttlMinutes}"],
            "codeTtlSec": 300,
            "resendCooldownSec": 60,
            "maxSendsPerHour": 5,
            "maxVerifyAttempts": 3,
        },
        "secret_references_present": {"secretId": True, "secretKey": True},
        "updated_at": 17,
    }
    assert all(
        marker not in response.text
        for marker in (
            "sms.original-version",
            "fixture-original-key",
            "active_version",
            "ready",
            "original-writer",
        )
    )
    assert (
        vault.calls == []
        and sessions.writer.calls == calls
        and sessions.path.read_bytes() == original
    )


async def test_put_preserves_actual_history_and_never_returns_private_request_values(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    body = dict(_BODY) | {"secrets": {"secretKey": "fixture-private-rotation"}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, service)), base_url="http://test"
    ) as client:
        response = await client.put(_URL, json=body, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200 and response.json() == {
        "version": "frozen-next-version",
        "restart_required": True,
    }
    assert (
        response.headers["cache-control"] == "no-store"
        and "fixture-private-rotation" not in response.text
    )
    assert vault.values["sms.frozen-next-version.secretKey"] == "fixture-private-rotation"
    rows = _rows(sessions)
    assert json.loads(rows["platform.v1.sms"][0])["updatedBy"] == _IDS["homeMember"]
    assert (
        json.loads(rows["platform.history.sms.original-version"][0])["version"]
        == "original-version"
    )


async def test_absent_managed_record_stays_absent_and_takeover_is_explicitly_unavailable(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch, seed=False)
    before = _rows(sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, service)), base_url="http://test"
    ) as client:
        response = await client.get(_URL, headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200 and response.json() == {
            "managed": False,
            "saved_version": None,
            "configuration": None,
            "secret_references_present": {"secretId": False, "secretKey": False},
            "updated_at": None,
        }
        saved = await client.put(
            _URL,
            json={"expected_version": None, "configuration": {}},
            headers={"Authorization": "Bearer " + token},
        )
    assert saved.status_code == 503 and vault.calls == [] and _rows(sessions) == before


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize(("role", "scopes"), [("admin", ["*"]), ("super_admin", ["admin:users"])])
async def test_current_super_admin_and_signed_platform_ceiling_apply_to_both_operations(
    sessions: _Environment,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    role: str,
    scopes: list[str],
) -> None:
    service, vault, _ = await _prepared(sessions, monkeypatch)
    _write(sessions, "UPDATE users SET role=? WHERE id=?", (role, _IDS["homeMember"]))
    token = _signed(await _login(sessions), scopes=scopes)
    calls, original = sessions.writer.calls, _rows(sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, service)), base_url="http://test"
    ) as client:
        response = await client.request(
            method,
            _URL,
            json=_BODY if method == "PUT" else None,
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
    assert vault.calls == [] and sessions.writer.calls == calls and _rows(sessions) == original


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"configuration": {}},
        {"expected_version": 1, "configuration": {}},
        dict(_BODY) | {"organization_id": "foreign"},
        dict(_BODY) | {"owner_id": "foreign"},
        dict(_BODY) | {"access_token": "private-body-marker"},
        dict(_BODY) | {"secrets": {"secretKey": ["private-body-marker"]}},
        dict(_BODY) | {"secrets": {"secretKey": 123}},
    ],
)
async def test_strict_body_and_secret_validation_redact_input(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    before, calls = _rows(sessions), sessions.writer.calls
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, service)), base_url="http://test"
    ) as client:
        response = await client.put(_URL, json=body, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 422 and "private-body-marker" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert vault.calls == [] and _rows(sessions) == before and sessions.writer.calls == calls


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_missing_duplicate_or_query_auth_never_reaches_service(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    before, calls = _rows(sessions), sessions.writer.calls
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, service)), base_url="http://test"
    ) as client:
        for headers, suffix, status in [
            ([], "", 401),
            ([("Authorization", "Bearer " + token)] * 2, "", 401),
            ([("Authorization", "Bearer " + token)], "?scope=platform", 400),
            ([], "?token=" + token, 401),
        ]:
            response = await client.request(
                method, _URL + suffix, json=_BODY if method == "PUT" else None, headers=headers
            )
            assert response.status_code == status and token not in response.text
    assert vault.calls == [] and _rows(sessions) == before and sessions.writer.calls == calls


@pytest.mark.parametrize("replacement", ["service", "snapshots"])
async def test_changed_actual_mount_refuses_before_metadata_or_vault_io(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    app = _app(sessions, service)
    if replacement == "service":
        app.state.canonical_sms_configuration = object()
    else:
        app.state.canonical_identity = object()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for method in ["GET", "PUT"]:
            response = await client.request(
                method,
                _URL,
                json=_BODY if method == "PUT" else None,
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 503
    assert vault.calls == []


async def test_actor_revocation_during_metadata_read_withholds_original_metadata(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    original = service._reader.read_saved

    async def revoke() -> Any:
        result = await original()
        _write(sessions, "UPDATE users SET membership_revision=9 WHERE id=?", (_IDS["homeMember"],))
        return result

    monkeypatch.setattr(service._reader, "read_saved", revoke)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, service)), base_url="http://test"
    ) as client:
        response = await client.get(_URL, headers={"Authorization": "Bearer " + token})
    assert (
        response.status_code == 401
        and "original-version" not in response.text
        and vault.calls == []
    )


async def test_metadata_change_while_reading_does_not_return_a_mixed_snapshot(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    original = service._reader.read_saved
    count = 0

    async def change() -> Any:
        nonlocal count
        result = await original()
        count += 1
        if count == 1:
            with sqlite3.connect(sessions.path) as db:
                db.execute(
                    "UPDATE platform_integration_settings SET value_json=? WHERE setting_key='platform.v1.sms'",
                    (
                        json.dumps(
                            {
                                "version": "other",
                                "config": {},
                                "secretRefs": {},
                                "updatedBy": "other",
                                "updatedAt": 99,
                            }
                        ),
                    ),
                )
        return result

    monkeypatch.setattr(service._reader, "read_saved", change)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, service)), base_url="http://test"
    ) as client:
        response = await client.get(_URL, headers={"Authorization": "Bearer " + token})
    assert (
        response.status_code == 503
        and "original-version" not in response.text
        and vault.calls == []
    )


async def test_mount_withdrawal_after_commit_retains_actual_saved_result_without_ack(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    app = _app(sessions, service)
    original = service._publish

    def withdraw(*args: Any) -> None:
        original(*args)
        app.state.canonical_sms_configuration = None

    monkeypatch.setattr(service, "_publish", withdraw)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(_URL, json=_BODY, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 503 and "frozen-next-version" not in response.text
    assert json.loads(_rows(sessions)["platform.v1.sms"][0])["version"] == "frozen-next-version"
    assert sum(op == "put" for op, key in vault.calls) == 2


async def test_schema_is_explicit_and_secret_body_repr_is_redacted(
    sessions: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, vault, token = await _prepared(sessions, monkeypatch)
    schema = _app(sessions, service).openapi()
    assert set(schema["paths"][_URL]) == {"get", "put"}
    body = schema["components"]["schemas"]["ManagedSmsConfigurationBody"]
    assert body["additionalProperties"] is False and set(body["required"]) == {
        "expected_version",
        "configuration",
    }
    secret_schema = body["properties"]["secrets"]["additionalProperties"]["anyOf"][0]
    assert secret_schema["writeOnly"] is True and secret_schema["format"] == "password"
    value = ManagedSmsConfigurationBody(
        expected_version="v", configuration={}, secrets={"secretKey": "private-repr-marker"}
    )
    assert "private-repr-marker" not in repr(value)
