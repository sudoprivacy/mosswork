"""Exercise the proposed one-mount patch against frozen PC3, with fixture ports."""

import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.routing import iter_route_contexts

from tests.unit.identity.test_sms_configuration import _SCHEMA
from tests.unit.identity.test_sms_configuration_updates import _ORACLE, _Vault
from tests.unit.test_canonical_product_startup import (
    _platform_actor,
    _policy_foreground,
)
from tests.unit.test_canonical_product_startup import (
    configuration as configuration,
)
from tests.unit.test_canonical_product_startup import (
    environment as environment,
)
from tests.unit.test_canonical_product_startup import (
    isolated_boot as isolated_boot,
)
from tests.unit.test_canonical_product_startup import (
    legacy_db as legacy_db,
)
from tests.unit.test_canonical_product_startup import (
    product as product,
)

URL = "/api/identity/platform/sms-configuration"

for name in [
    "octop.launch",
    "octop.api.deps",
    "octop.infra.execution.composition",
    "octop.infra.identity.composition",
]:
    assert Path(sys.modules[name].__file__).is_relative_to(Path(__file__).parents[2] / "src"), name


def seed(environment):
    with sqlite3.connect(environment.path) as db:
        db.executescript(_SCHEMA)
        db.execute(
            "INSERT INTO platform_integration_settings VALUES (?,?,?,7,8)",
            ("platform.v1.sms", json.dumps(_ORACLE["original"]), "original-writer"),
        )


def stored(environment):
    with sqlite3.connect(environment.path) as db:
        return db.execute(
            "SELECT * FROM platform_integration_settings ORDER BY setting_key"
        ).fetchall()


@pytest.fixture
def sms_dashboard(tmp_path, monkeypatch):
    dashboard = tmp_path / "fixture-source" / "octop" / "dashboard"
    dashboard.mkdir(parents=True)
    (dashboard / "index.html").write_text("<!doctype html><title>SMS mount fixture</title>")
    monkeypatch.setattr("octop.api.app.__file__", str(dashboard.parent / "api" / "app.py"))


def test_exact_constructor_forwards_existing_vault_without_io(configuration):
    vault = _Vault()
    selected = replace(configuration, sms_configuration_vault=vault)
    assert selected.sms_configuration_vault is vault
    assert vault.calls == [] and selected.backend_factory.inputs == []


@pytest.mark.parametrize(
    "vault", [False, object(), type("ReadOnly", (), {"get_secret": lambda: None})()]
)
def test_incomplete_vault_refuses_before_product_boot(configuration, vault):
    with pytest.raises(TypeError, match="trusted platform SMS vault"):
        replace(configuration, sms_configuration_vault=vault)
    assert configuration.backend_factory.inputs == []


@pytest.mark.parametrize("configured", [False, True])
async def test_one_foreground_mount_same_business_and_exact_method_gate(
    configuration, isolated_boot, environment, monkeypatch, configured, sms_dashboard
):
    seed(environment)
    vault = _Vault()
    selected = (
        replace(configuration, sms_configuration_vault=vault) if configured else configuration
    )
    monkeypatch.setattr(
        "octop.infra.identity.sms_configuration_updates.uuid4", lambda: "mount-next-version"
    )

    async def exercise(app, server):
        business = server.canonical.business
        service = business.managed_sms_configuration
        token = (await _platform_actor(environment, business)).access_token
        headers = {"Authorization": "Bearer " + token}
        routes = [item for item in iter_route_contexts(app.routes) if item.path == URL]
        before = stored(environment)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert any(getattr(route, "name", None) == "spa_fallback" for route in app.routes)
            if not configured:
                assert service is None and routes == []
                assert not hasattr(app.state, "canonical_sms_configuration")
                for method in ["GET", "PUT"]:
                    response = await client.request(method, URL, headers=headers, json={})
                    assert response.status_code == 503
                    assert response.json() == {"canonical_resource_unavailable": True}
                assert stored(environment) == before and vault.calls == []
                return
            assert app.state.canonical_sms_configuration is service
            assert service._snapshots is app.state.canonical_identity is business.snapshots
            assert service._vault is vault
            assert len(routes) == 2 and {m for route in routes for m in route.methods} == {
                "GET",
                "PUT",
            }
            assert len(business.app_kwargs()) == 11
            assert len(vars(server.canonical.runtime_inputs)) == 5
            get = await client.get(URL, headers=headers)
            assert get.status_code == 200 and get.json()["saved_version"] == "original-version"
            assert get.headers["cache-control"] == "no-store" and vault.calls == []
            for method, path in [
                ("POST", URL),
                ("DELETE", URL),
                ("PATCH", URL),
                ("GET", URL + "/child"),
            ]:
                refused = await client.request(method, path, headers=headers, json={})
                assert refused.status_code == 503 and refused.json() == {
                    "canonical_resource_unavailable": True
                }
            for credentials in [[], [("Authorization", "Bearer " + token)] * 2]:
                assert (await client.get(URL, headers=credentials)).status_code == 401
            assert (await client.get(URL + "?scope=platform", headers=headers)).status_code == 400
            assert vault.calls == [] and stored(environment) == before
            put = await client.put(
                URL,
                headers=headers,
                json={"expected_version": "original-version", "configuration": {}},
            )
            assert put.status_code == 200 and put.json() == {
                "version": "mount-next-version",
                "restart_required": True,
            }
            assert put.headers["cache-control"] == "no-store"
            read = await client.get(URL, headers=headers)
            assert read.status_code == 200 and read.json()["saved_version"] == "mount-next-version"
            assert "fixture-original-key" not in read.text and "active_version" not in read.text
            assert len([op for op, _ in vault.calls if op == "put"]) == 2
            assert len(stored(environment)) == 2
            assert server.user_manager.count() == 0

    await _policy_foreground(selected, isolated_boot, monkeypatch, exercise)


@pytest.mark.parametrize("replacement", ["service", "snapshot", "configured-business"])
async def test_changed_binding_refuses_inside_actual_main_middleware(
    configuration, isolated_boot, environment, monkeypatch, replacement
):
    seed(environment)
    vault = _Vault()
    selected = replace(configuration, sms_configuration_vault=vault)

    async def exercise(app, server):
        token = (await _platform_actor(environment, server.canonical.business)).access_token
        if replacement == "service":
            app.state.canonical_sms_configuration = object()
        elif replacement == "snapshot":
            app.state.canonical_identity = object()
        else:
            server.canonical = replace(
                server.canonical,
                business=replace(server.canonical.business, managed_sms_configuration=object()),
            )
        before, calls = stored(environment), environment.writer.calls
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for method in ["GET", "PUT"]:
                response = await client.request(
                    method,
                    URL,
                    headers={"Authorization": "Bearer " + token},
                    json={"expected_version": "original-version", "configuration": {}},
                )
                assert response.status_code == 503
                assert response.json() == {"canonical_resource_unavailable": True}
        assert (
            vault.calls == []
            and stored(environment) == before
            and environment.writer.calls == calls
        )

    await _policy_foreground(selected, isolated_boot, monkeypatch, exercise)


async def test_postcommit_withdrawal_keeps_original_write_without_ack_or_retry(
    configuration, isolated_boot, environment, monkeypatch
):
    seed(environment)
    vault = _Vault()
    selected = replace(configuration, sms_configuration_vault=vault)
    monkeypatch.setattr(
        "octop.infra.identity.sms_configuration_updates.uuid4", lambda: "committed-mount-version"
    )

    async def exercise(app, server):
        service = server.canonical.business.managed_sms_configuration
        token = (await _platform_actor(environment, server.canonical.business)).access_token
        original = service._publish

        def withdraw(*args, **kwargs):
            result = original(*args, **kwargs)
            app.state.canonical_sms_configuration = None
            return result

        monkeypatch.setattr(service, "_publish", withdraw)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                URL,
                headers={"Authorization": "Bearer " + token},
                json={"expected_version": "original-version", "configuration": {}},
            )
        assert response.status_code == 503
        rows = {row[0]: row for row in stored(environment)}
        assert json.loads(rows["platform.v1.sms"][1])["version"] == "committed-mount-version"
        assert "platform.history.sms.original-version" in rows
        assert len([op for op, _ in vault.calls if op == "put"]) == 2

    await _policy_foreground(selected, isolated_boot, monkeypatch, exercise)
