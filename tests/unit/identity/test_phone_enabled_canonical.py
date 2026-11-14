"""Retain effective SMS disable without a second or disabled password issuer."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.support.app import mosswork_client, write_mosswork_config
from tests.unit.identity.test_canonical_business_composition import (
    _IDS,
    _NUMBER,
    _PASSWORD,
    _Environment,
    _login,
)
from tests.unit.identity.test_canonical_business_composition import environment as environment
from tests.unit.identity.test_canonical_business_composition import legacy_db as legacy_db
from tests.unit.identity.test_canonical_business_composition import product as product

from octop.api.app import build_app
from octop.infra.errors import MossworkError
from octop.infra.identity.composition import build_canonical_business


def _disabled(environment: _Environment):
    return build_canonical_business(
        environment.path,
        **{
            **environment.kwargs,
            "phone_config": None,
            "sms_sender": None,
            "phone_enabled": lambda: False,
        },
    )


async def test_disabled_sms_needs_no_sender_and_keeps_real_password_and_refresh(
    environment: _Environment,
) -> None:
    bundle = _disabled(environment)
    assert bundle.phone is None and bundle.password._issuer is bundle.sessions
    assert bundle.api_key_exchange._issuer is bundle.sessions
    with sqlite3.connect(environment.path) as db:
        db.execute(
            "INSERT OR REPLACE INTO client_delivery_policies VALUES ('platform','default',?,'fixture',1,1)",
            ('{"loginMethod":"password"}',),
        )
        db.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (_PASSWORD["passwordHash"], _IDS["homeMember"]),
        )
    pair = await bundle.password.login_sudowork(username="home", password=_PASSWORD["password"])
    assert (await bundle.snapshots.snapshot(pair.access_token)).key_id == "password-login"
    refresh = await bundle.sessions.refresh(pair.refresh_token)
    assert (await bundle.snapshots.snapshot(refresh.access_token)).key_id == "password-login"
    before = environment.path.read_bytes()
    for operation in (bundle.sessions.send_code(_NUMBER), bundle.sessions.login(_NUMBER, "123456")):
        with pytest.raises(MossworkError) as error:
            await operation
        assert error.value.status == 404
    assert environment.path.read_bytes() == before and environment.sender.sent == []


async def test_existing_phone_human_tokens_switch_and_refresh_survive_new_sms_disable(
    environment: _Environment,
) -> None:
    pair = await _login(environment)
    bundle = _disabled(environment)
    original = await bundle.snapshots.snapshot(pair.access_token)
    assert original.member.owner_id == _IDS["homeMember"]
    refreshed = await bundle.sessions.refresh(pair.refresh_token)
    assert (
        await bundle.snapshots.snapshot(refreshed.access_token)
    ).account_id == original.account_id
    switched = await bundle.sessions.switch_organization(pair.access_token, _IDS["companyOrg"])
    assert (await bundle.snapshots.snapshot(switched.access_token)).member.owner_id == _IDS[
        "companyMember"
    ]
    await bundle.sessions.logout(pair.access_token, pair.refresh_token)
    with pytest.raises(MossworkError):
        await bundle.snapshots.snapshot(pair.access_token)


async def test_disabled_sms_keeps_moss_password_under_original_sms_org_policy(
    environment: _Environment,
) -> None:
    bundle = _disabled(environment)
    with sqlite3.connect(environment.path) as db:
        db.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (_PASSWORD["passwordHash"], _IDS["homeMember"]),
        )
    with pytest.raises(MossworkError) as denied:
        await bundle.password.login_sudowork(username="home", password=_PASSWORD["password"])
    assert denied.value.status == 403
    pair = await bundle.password.login_moss(username="home", password=_PASSWORD["password"])
    snapshot = await bundle.snapshots.snapshot(pair.access_token)
    assert snapshot.auth_app == "moss" and snapshot.key_id == "moss-password-login"
    refreshed = await bundle.sessions.refresh(pair.refresh_token)
    assert (await bundle.snapshots.snapshot(refreshed.access_token)).auth_app == "moss"
    assert environment.sender.sent == []


@pytest.mark.parametrize("value", [None, 1, "false"])
async def test_malformed_effective_setting_refuses_before_writer_or_sms(
    environment: _Environment, value: Any
) -> None:
    bundle = build_canonical_business(
        environment.path, **{**environment.kwargs, "phone_enabled": lambda: value}
    )
    calls = environment.writer.calls
    with pytest.raises(MossworkError) as error:
        await bundle.sessions.send_code(_NUMBER)
    assert error.value.status == 503
    assert environment.writer.calls == calls and environment.sender.sent == []


async def test_dynamic_effective_disable_uses_original_service_without_rebuilding(
    environment: _Environment,
) -> None:
    enabled = [True]
    bundle = build_canonical_business(
        environment.path, **{**environment.kwargs, "phone_enabled": lambda: enabled[0]}
    )
    assert bundle.sessions.is_phone_login_available
    enabled[0] = False
    assert not bundle.sessions.is_phone_login_available
    calls = environment.writer.calls
    with pytest.raises(MossworkError) as error:
        await bundle.sessions.send_code(_NUMBER)
    assert error.value.status == 404 and environment.writer.calls == calls
    enabled[0] = True
    await bundle.sessions.send_code(_NUMBER)
    assert len(environment.sender.sent) == 1


async def test_disable_during_writer_await_refuses_before_phone_side_effect(
    environment: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    enabled = [True]
    bundle = build_canonical_business(
        environment.path, **{**environment.kwargs, "phone_enabled": lambda: enabled[0]}
    )

    async def disable() -> None:
        enabled[0] = False

    monkeypatch.setattr(environment.writer, "assert_current_writer", disable)
    before = environment.path.read_bytes()
    with pytest.raises(MossworkError) as error:
        await bundle.sessions.send_code(_NUMBER)
    assert error.value.status == 404
    assert environment.sender.sent == [] and environment.path.read_bytes() == before


async def test_enabled_setting_without_actual_sms_ports_is_unavailable(
    environment: _Environment,
) -> None:
    bundle = build_canonical_business(
        environment.path,
        **{
            **environment.kwargs,
            "phone_config": None,
            "sms_sender": None,
            "phone_enabled": lambda: True,
        },
    )
    with pytest.raises(MossworkError) as error:
        await bundle.sessions.send_code(_NUMBER)
    assert error.value.status == 503 and environment.sender.sent == []


async def test_public_status_reads_current_setting_and_password_http_stays_available(
    environment: _Environment, tmp_path: Path
) -> None:
    enabled: list[Any] = [False]
    bundle = build_canonical_business(
        environment.path, **{**environment.kwargs, "phone_enabled": lambda: enabled[0]}
    )
    with sqlite3.connect(environment.path) as db:
        db.execute(
            "INSERT OR REPLACE INTO client_delivery_policies VALUES ('platform','default',?,'fixture',1,1)",
            ('{"loginMethod":"password"}',),
        )
        db.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (_PASSWORD["passwordHash"], _IDS["homeMember"]),
        )
    home = tmp_path / "product-runtime"
    write_mosswork_config(home, enable_api_docs=True, enable_dashboard=False)
    async with mosswork_client(home) as (_, server):
        app = build_app(server, **bundle.app_kwargs())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            status = await client.get("/api/identity/status")
            assert status.json() == {
                "mode": "canonical",
                "phone_login_available": False,
                "password_login_available": True,
            }
            assert status.headers["cache-control"] == "no-store"
            refused = await client.post("/api/identity/phone/send-code", json={"phone": _NUMBER})
            assert refused.status_code == 404
            password = await client.post(
                "/api/identity/password/login",
                json={"username": "home", "password": _PASSWORD["password"]},
            )
            assert password.status_code == 200
            token = password.json()["access_token"]
            assert (await bundle.snapshots.snapshot(token)).key_id == "password-login"
            enabled[0] = True
            assert (await client.get("/api/identity/status")).json()[
                "phone_login_available"
            ] is True
            enabled[0] = "malformed"
            unavailable = await client.get("/api/identity/status")
            assert (
                unavailable.status_code == 503
                and unavailable.headers["cache-control"] == "no-store"
            )
            assert "malformed" not in unavailable.text
            assert server.user_manager is not None and server.user_manager.count() == 0
            assert "/api/identity/phone/send-code" in app.openapi()["paths"]


async def test_current_setting_provider_failure_is_sanitized(environment: _Environment) -> None:
    def unavailable() -> bool:
        raise RuntimeError("sensitive-config-marker")

    bundle = build_canonical_business(
        environment.path, **{**environment.kwargs, "phone_enabled": unavailable}
    )
    with pytest.raises(MossworkError) as error:
        await bundle.sessions.send_code(_NUMBER)
    assert error.value.status == 503 and "sensitive-config-marker" not in str(error.value)
    assert environment.sender.sent == []
