"""Real Product boot/HTTP/queue; backend, SMS and native grants are fixtures."""

import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from octop import launch
from octop.api.app import build_app
from octop.api.deps import resolve_user_from_token
from octop.config import DatabaseConfig, MossworkConfig, load_config
from octop.infra.db.rebind import rebind_control_plane
from octop.infra.errors import MossworkError
from octop.infra.execution.composition import CanonicalProductConfiguration
from octop.infra.execution.service import current_execution
from octop.infra.identity.request_principal import authenticate
from octop.infra.server import MossworkServer
from tests.unit.agents.test_managed_execution_backend import FixtureBackend
from tests.unit.identity.test_canonical_business_composition import environment as environment
from tests.unit.identity.test_canonical_business_composition import legacy_db as legacy_db
from tests.unit.identity.test_canonical_business_composition import product as product
from tests.unit.identity.test_human_sessions import _NUMBER


class Backend(FixtureBackend):
    def __init__(self):
        super().__init__()
        self.assert_chat_current = AsyncMock(
            side_effect=AssertionError("This startup fixture supplies no resource grants")
        )

    async def stream(self, agent, request, *, admission=None):
        active = current_execution.get()
        assert active is not None
        assert await active.canonical_principal() is admission
        self.streams.append((agent, request, admission, active.run["run_id"]))
        yield {"type": "token", "content": "fixture output"}


class BackendFactory:
    def __init__(self):
        self.backend = Backend()
        self.inputs = []

    def __call__(self, inputs):
        self.inputs.append(inputs)
        return self.backend


@pytest.fixture
def configuration(environment):
    kwargs = dict(environment.kwargs)
    kwargs.pop("product_projections")
    return CanonicalProductConfiguration(
        database_path=environment.path,
        backend_factory=BackendFactory(),
        node="product-fixture-controller",
        incarnation="product-fixture-boot",
        capacity=1,
        **kwargs,
    )


@pytest.fixture
def isolated_boot(monkeypatch, tmp_path):
    home = tmp_path / "product-home"
    monkeypatch.setenv("MOSSWORK_HOME", str(home))
    for key in list(os.environ):
        if key.startswith("MOSSWORK_DATABASE_"):
            monkeypatch.delenv(key)
    # Installation and capability probing are outside this startup qualification.
    monkeypatch.setattr("octop.infra.server.ensure_mobile_capabilities_probed", load_config)
    monkeypatch.setattr("octop.infra.server.PluginManager.seed_bundled", Mock())
    monkeypatch.setattr("octop.infra.server.PluginManager.load_installed", Mock())
    return home


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("alias", ["same", "hardlink", "wal", "shm", "journal"])
async def test_alias_refuses_before_pool_migration_or_provider(
    configuration, isolated_boot, monkeypatch, alias
):
    path = configuration.database_path
    if alias == "same":
        selected = path
    elif alias == "hardlink":
        selected = path.parent / "different-product-name.db"
        selected.hardlink_to(path)
    else:
        selected = Path(str(path) + "-" + alias)
    isolated_boot.mkdir()
    (isolated_boot / "config.json").write_text(
        json.dumps({"database": {"driver": "sqlite", "sqlite_path": str(selected)}}),
        encoding="utf-8",
    )
    opened = Mock(side_effect=AssertionError("Product pool must not open"))
    monkeypatch.setattr("octop.infra.server.open_database", opened)
    before = digest(path)
    server = MossworkServer(isolated_boot, canonical_configuration=configuration)
    with pytest.raises(ValueError, match="physically separate"):
        await server.start()
    opened.assert_not_called()
    configuration.backend_factory.backend.boot.assert_not_called()
    configuration.backend_factory.backend.aclose.assert_not_called()
    assert digest(path) == before and server.services is None


@pytest.mark.parametrize("product_suffix", ["", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize("canonical_suffix", ["", "-wal", "-shm", "-journal"])
async def test_all_product_file_aliases_refuse_before_pool_open(
    configuration, isolated_boot, monkeypatch, product_suffix, canonical_suffix
):
    canonical = configuration.database_path
    protected = [Path(str(canonical) + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    for path in protected[1:]:
        path.write_bytes(b"original retained sidecar")
    before = {path: digest(path) for path in protected}
    isolated_boot.mkdir()
    selected = isolated_boot / "product.db"
    Path(str(selected) + product_suffix).hardlink_to(Path(str(canonical) + canonical_suffix))
    (isolated_boot / "config.json").write_text(
        json.dumps({"database": {"driver": "sqlite", "sqlite_path": str(selected)}}),
        encoding="utf-8",
    )
    opened = Mock(side_effect=AssertionError("Product pool must not open"))
    monkeypatch.setattr("octop.infra.server.open_database", opened)
    server = MossworkServer(isolated_boot, canonical_configuration=configuration)
    with pytest.raises(ValueError, match="physically separate"):
        await server.start()
    opened.assert_not_called()
    configuration.backend_factory.backend.boot.assert_not_called()
    assert {path: digest(path) for path in protected} == before
    assert server.services is None


def test_distinct_product_and_canonical_sidecars_are_allowed(configuration, isolated_boot):
    isolated_boot.mkdir()
    selected = isolated_boot / "product.db"
    protected = []
    for main in (configuration.database_path, selected):
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(main) + suffix)
            if not path.exists():
                path.write_bytes(b"independent retained file")
            protected.append(path)
    before = {path: digest(path) for path in protected}
    config = MossworkConfig(
        database=DatabaseConfig(driver="sqlite", sqlite_path=str(selected)), database_in_file=True
    )
    server = MossworkServer(isolated_boot)
    assert configuration.validate_storage(config, server.paths) == selected.resolve()
    assert {path: digest(path) for path in protected} == before


async def test_missing_original_refuses_before_creating_product(
    configuration, isolated_boot, monkeypatch
):
    configuration = replace(configuration, database_path=isolated_boot.parent / "missing.db")
    opened = Mock(side_effect=AssertionError("Product pool must not open"))
    monkeypatch.setattr("octop.infra.server.open_database", opened)
    server = MossworkServer(isolated_boot, canonical_configuration=configuration)
    with pytest.raises(ValueError, match="Cannot verify"):
        await server.start()
    opened.assert_not_called()
    assert not server.paths.db.exists()


def test_storage_observation_error_refuses(configuration, isolated_boot, monkeypatch):
    original_stat = Path.stat
    selected = MossworkServer(isolated_boot).paths.db

    def stat(path, *args, **kwargs):
        if path == selected:
            raise PermissionError("fixture inaccessible Product path")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(ValueError, match="Cannot verify"):
        configuration.validate_storage(MossworkConfig(), MossworkServer(isolated_boot).paths)


async def test_migration_error_closes_open_pool_and_preserves_original(
    configuration, isolated_boot, monkeypatch
):
    import octop.infra.server as server_module

    original = RuntimeError("fixture Product migration failed")
    opened = []
    open_database = server_module.open_database

    def open_pool(*args):
        pool = open_database(*args)
        opened.append(pool)
        return pool

    monkeypatch.setattr(server_module, "open_database", open_pool)
    monkeypatch.setattr(server_module, "run_migrations", Mock(side_effect=original))
    server = MossworkServer(isolated_boot, canonical_configuration=configuration)
    with pytest.raises(RuntimeError) as failure:
        await server.start()
    assert failure.value is original and len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"), opened[0].connect() as connection:
        connection.execute("SELECT 1")
    configuration.backend_factory.backend.boot.assert_not_called()


def test_missing_backend_and_non_sqlite_product_refuse(configuration, isolated_boot):
    with pytest.raises(TypeError, match="managed backend factory"):
        replace(configuration, backend_factory=None)
    with pytest.raises(ValueError, match="SQLite Product"):
        configuration.validate_storage(
            MossworkConfig(database=DatabaseConfig(driver="postgresql")),
            MossworkServer(isolated_boot).paths,
        )


@pytest.mark.parametrize("role", ["init", "web", "worker"])
async def test_unsupported_role_refuses_before_role_boot(configuration, role):
    with pytest.raises(ValueError, match="one monolith"):
        await launch.run_foreground(
            host=None,
            port=None,
            reload=False,
            workers=1,
            log_level=None,
            ssl_certfile=None,
            ssl_keyfile=None,
            role=role,
            canonical_configuration=configuration,
        )
    configuration.backend_factory.backend.boot.assert_not_called()


@pytest.mark.parametrize("workers,reload", [(2, False), (1, True)])
async def test_multiple_processes_or_reload_refuse(configuration, workers, reload):
    with pytest.raises(ValueError, match="one monolith"):
        await launch.run_foreground(
            host=None,
            port=None,
            reload=reload,
            workers=workers,
            log_level=None,
            ssl_certfile=None,
            ssl_keyfile=None,
            canonical_configuration=configuration,
        )


async def start(configuration, home):
    server = MossworkServer(home, canonical_configuration=configuration)
    await server.start()
    return server


async def test_actual_boot_wires_one_bundle_pool_queue_and_private_actor(
    configuration, isolated_boot, monkeypatch
):
    wizard = Mock(side_effect=AssertionError("local admin setup must not run"))
    monkeypatch.setattr(MossworkServer, "_emit_wizard_password", wizard)
    before = digest(configuration.database_path)
    server = await start(configuration, isolated_boot)
    try:
        bound = server.canonical
        assert bound is not None
        assert digest(configuration.database_path) == before
        assert server.services.secret_repo.get("jwt") is None
        assert server.user_manager.count() == 0
        assert server.execution is bound.execution
        assert bound.execution.repo is bound.admissions.repo
        assert bound.execution.human_admissions is bound.admissions
        assert bound.admissions.repo.db is server.services.db
        assert bound.business.requests._projections._db is server.services.db
        assert configuration.backend_factory.inputs == [bound.runtime_inputs]
        assert bound.runtime_inputs.business is bound.business
        assert bound.runtime_inputs.services is server.services
        assert bound.runtime_inputs.execution is server.execution
        assert bound.runtime_inputs.selections._originals is bound.runtime_inputs.originals
        manager = server.app_runtime.agent_registry
        assert manager._managed_backend is configuration.backend_factory.backend
        assert manager.execution is server.app_runtime.gateway.processor.execution
        assert manager.execution.service is server.execution
        app = build_app(
            server, **bound.business.app_kwargs(), canonical_admissions=bound.admissions
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/identity/status")).json()["mode"] == "canonical"
            assert (
                await client.post("/api/identity/phone/send-code", json={"phone": _NUMBER})
            ).status_code == 200
            response = await client.post(
                "/api/identity/phone/login",
                json={"phone": _NUMBER, "code": bound.business.phone._sms_sender.sent[-1][1]},
            )
            assert response.status_code == 200
            token = response.json()["access_token"]
            assert (
                await client.get("/api/identity/me", headers={"Authorization": "Bearer " + token})
            ).status_code == 200
        principal = await authenticate(bound.business.requests, token)
        user = principal.identity.projection.local_user_id
        server.services.agent_repo.create(agent_id="original-agent", user_id=user, name="Original")
        server.services.thread_repo.insert(
            thread_id="original-thread",
            agent_id="original-agent",
            user_id=user,
            channel_type="dashboard",
            session_key="original-existing-reference",
        )
        frames = [
            item
            async for item in manager.stream(
                "original-agent",
                {"thread_id": "original-thread", "messages": []},
                admission=principal,
            )
        ]
        assert any(frame.get("content") == "fixture output" for frame in frames)
        sent = configuration.backend_factory.backend.streams
        assert len(sent) == 1 and sent[0][2] is principal
        run = bound.execution.repo.get(sent[0][3])
        assert run["state"] == "completed" and token not in run["request_json"]
        wizard.assert_not_called()
    finally:
        await server.stop()
    configuration.backend_factory.backend.aclose.assert_awaited_once()


@pytest.mark.parametrize("configured", [False, True])
async def test_same_startup_passes_only_explicit_login_policy_readiness(
    configuration, isolated_boot, configured
):
    readiness = SimpleNamespace(assert_available=AsyncMock())
    selected = (
        replace(configuration, login_policy_readiness=readiness) if configured else configuration
    )
    before = digest(configuration.database_path)
    server = await start(selected, isolated_boot)
    try:
        bound = server.canonical
        service = bound.business.organization_login_policy
        assert len(bound.business.app_kwargs()) == 11
        assert "canonical_organization_login_policy" not in bound.business.app_kwargs()
        assert selected.backend_factory.inputs == [bound.runtime_inputs]
        assert bound.runtime_inputs.business is bound.business
        assert set(vars(bound.runtime_inputs)) == {
            "business",
            "services",
            "originals",
            "selections",
            "execution",
        }
        assert digest(configuration.database_path) == before
        if configured:
            assert service is not None
            assert service._readiness is selected.login_policy_readiness is readiness
            assert (
                service._snapshots is bound.business.snapshots is bound.business.sessions.snapshots
            )
            assert service._writer is selected.writer
            assert service._clock is selected.clock
            assert service._write_uri == selected.database_path.as_uri() + "?mode=rw"
        else:
            assert selected.login_policy_readiness is None
            assert service is None
        readiness.assert_available.assert_not_awaited()
    finally:
        await server.stop()


@pytest.mark.parametrize("readiness", [False, True, object(), lambda: True])
def test_malformed_login_readiness_refuses_configuration_before_product_open(
    configuration, monkeypatch, readiness
):
    opened = Mock(side_effect=AssertionError("Product pool must not open"))
    monkeypatch.setattr("octop.infra.server.open_database", opened)
    before = digest(configuration.database_path)
    with pytest.raises(TypeError, match="readiness port"):
        replace(configuration, login_policy_readiness=readiness)
    assert digest(configuration.database_path) == before
    assert configuration.backend_factory.inputs == []
    opened.assert_not_called()


async def test_factory_missing_backend_refuses_without_local_fallback(configuration, isolated_boot):
    calls = []

    def unavailable(inputs):
        calls.append(inputs)
        return None

    server = MossworkServer(
        isolated_boot,
        canonical_configuration=replace(configuration, backend_factory=unavailable),
    )
    with pytest.raises(TypeError, match="must return a managed execution backend"):
        await server.start()
    assert len(calls) == 1 and server.services is None and not server._started
    assert server._managed_backend is None
    with pytest.raises(ValueError, match="same business bundle"):
        MossworkServer(
            isolated_boot,
            canonical_configuration=configuration,
            managed_backend=configuration.backend_factory.backend,
        )


@pytest.mark.parametrize("guard", ["missing", None, False, "not-a-current-grant"])
async def test_factory_requires_current_chat_authorization_before_boot(
    configuration, isolated_boot, guard
):
    backend = configuration.backend_factory.backend
    if guard == "missing":
        if hasattr(backend, "assert_chat_current"):
            del backend.assert_chat_current
    else:
        backend.assert_chat_current = guard
    before = digest(configuration.database_path)
    server = MossworkServer(isolated_boot, canonical_configuration=configuration)
    try:
        with pytest.raises(TypeError, match="managed execution backend"):
            await server.start()
        assert server.services is None and not server._started
        assert server._managed_backend is None
        backend.boot.assert_not_called()
        backend.start.assert_not_called()
        backend.call.assert_not_called()
        assert digest(configuration.database_path) == before
    finally:
        await server.stop()


async def test_local_routes_storage_rebind_and_local_ws_decoder_refuse(
    configuration, isolated_boot
):
    server = await start(configuration, isolated_boot)
    try:
        bound = server.canonical
        with pytest.raises(ValueError, match="configured business"):
            build_app(server)
        kwargs = bound.business.app_kwargs()
        kwargs["canonical_profile"] = None
        with pytest.raises(ValueError, match="configured business"):
            build_app(server, **kwargs, canonical_admissions=bound.admissions)
        app = build_app(
            server, **bound.business.app_kwargs(), canonical_admissions=bound.admissions
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for path in ("/api/setup/status", "/api/auth/login", "/api/agents"):
                result = await client.get(path)
                assert result.status_code == 503
                assert result.json() == {"canonical_resource_unavailable": True}
        with pytest.raises(MossworkError) as rejected:
            resolve_user_from_token(server, "even-a-previous-local-token")
        assert rejected.value.status == 503
        with pytest.raises(ValueError, match="local setup"):
            await server.bind_control_plane()
        with pytest.raises(ValueError, match="controlled restart"):
            rebind_control_plane(server)
    finally:
        await server.stop()


async def test_provider_boot_error_preserves_original_and_closes_owned_resources(
    configuration, isolated_boot
):
    original = RuntimeError("fixture provider rejection")
    configuration.backend_factory.backend.boot.side_effect = original
    configuration.backend_factory.backend.aclose.side_effect = RuntimeError(
        "fixture withdrawal failed"
    )
    server = MossworkServer(isolated_boot, canonical_configuration=configuration)
    with pytest.raises(RuntimeError) as failure:
        await server.start()
    assert failure.value is original
    assert "Canonical startup resource withdrawal also failed" in original.__notes__
    assert server.services is None and not server._started
    configuration.backend_factory.backend.aclose.assert_awaited_once()


async def test_existing_foreground_passes_the_configured_bundle_to_http(
    configuration, isolated_boot, monkeypatch
):
    observed = []

    async def serve(http_server):
        app = http_server.config.app
        server = app.state.mosswork_server
        observed.append(server)
        assert app.state.canonical_requests is server.canonical.business.requests
        assert app.state.canonical_admissions is server.execution.human_admissions
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/identity/status")).json()["mode"] == "canonical"

    monkeypatch.setattr(launch, "_serve", serve)
    monkeypatch.setattr(launch, "_schedule_linux_bubblewrap_ensure", lambda: None)
    monkeypatch.setattr(
        "octop.infra.agents.memory.slim_control.MemorySlimControl.start", AsyncMock()
    )
    monkeypatch.setattr(
        "octop.infra.agents.memory.slim_control.MemorySlimControl.close", AsyncMock()
    )
    await launch.run_foreground(
        host=None,
        port=None,
        reload=False,
        workers=1,
        log_level=None,
        ssl_certfile=None,
        ssl_keyfile=None,
        canonical_configuration=configuration,
    )
    assert len(observed) == 1 and not observed[0]._started
    configuration.backend_factory.backend.boot.assert_awaited_once()
    configuration.backend_factory.backend.aclose.assert_awaited_once()


def _stored_login_policy(path):
    from tests.unit.identity.test_human_sessions import _IDS

    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT policy_json,updated_by FROM client_delivery_policies "
            "WHERE scope_type='organization' AND scope_id=?",
            (_IDS["homeOrg"],),
        ).fetchone()
    return (json.loads(row[0]), row[1]) if row is not None else None


async def _policy_administrator(environment, business):
    from tests.unit.identity.test_canonical_business_composition import _login
    from tests.unit.identity.test_human_sessions import _IDS

    with sqlite3.connect(environment.path) as db:
        db.execute("UPDATE users SET role='admin' WHERE id=?", (_IDS["homeMember"],))
    return (await _login(environment, business)).access_token


async def _policy_foreground(configuration, isolated_boot, monkeypatch, exercise):
    isolated_boot.mkdir(exist_ok=True)
    (isolated_boot / "config.json").write_text(
        json.dumps({"enable_api_docs": True}), encoding="utf-8"
    )
    served = []
    local_auth = Mock(side_effect=AssertionError("Policy routes must use canonical human auth"))
    monkeypatch.setattr("octop.api.middleware.jwt_auth.authenticate_request", local_auth)

    async def serve(http_server):
        app = http_server.config.app
        server = app.state.mosswork_server
        served.append(server)
        dispatches = {
            getattr(item.kwargs.get("dispatch"), "__name__", "") for item in app.user_middleware
        }
        assert {"_lockdown", "_jwt_auth"} <= dispatches
        assert server.user_manager.count() == 0
        assert app.state.canonical_requests is server.canonical.business.requests
        assert app.state.canonical_admissions is server.execution.human_admissions
        assert configuration.backend_factory.inputs == [server.canonical.runtime_inputs]
        await exercise(app, server)

    monkeypatch.setattr(launch, "_serve", serve)
    monkeypatch.setattr(launch, "_schedule_linux_bubblewrap_ensure", lambda: None)
    monkeypatch.setattr(
        "octop.infra.agents.memory.slim_control.MemorySlimControl.start", AsyncMock()
    )
    monkeypatch.setattr(
        "octop.infra.agents.memory.slim_control.MemorySlimControl.close", AsyncMock()
    )
    await launch.run_foreground(
        host=None,
        port=None,
        reload=False,
        workers=1,
        log_level=None,
        ssl_certfile=None,
        ssl_keyfile=None,
        canonical_configuration=configuration,
    )
    assert len(served) == 1 and not served[0]._started
    configuration.backend_factory.backend.boot.assert_awaited_once()
    configuration.backend_factory.backend.aclose.assert_awaited_once()
    configuration.backend_factory.backend.start.assert_not_called()
    configuration.backend_factory.backend.call.assert_not_called()
    local_auth.assert_not_called()


@pytest.mark.parametrize("configured", [False, True])
async def test_policy_same_foreground_mount_and_complete_middleware(
    configuration, isolated_boot, environment, monkeypatch, configured
):
    from fastapi.routing import iter_route_contexts

    from tests.unit.identity.test_human_sessions import _IDS
    from tests.unit.identity.test_organization_login_policy import _Readiness

    readiness = _Readiness()
    selected = (
        replace(configuration, login_policy_readiness=readiness) if configured else configuration
    )

    async def exercise(app, server):
        url = "/api/identity/organization/login-policy"
        bound = server.canonical
        service = bound.business.organization_login_policy
        assert len(bound.business.app_kwargs()) == 11
        assert set(vars(bound.runtime_inputs)) == {
            "business",
            "services",
            "originals",
            "selections",
            "execution",
        }
        policy_routes = [route for route in iter_route_contexts(app.routes) if route.path == url]
        assert len(policy_routes) == (2 if configured else 0)
        assert readiness.calls == []
        token = await _policy_administrator(environment, bound.business)
        headers = {"Authorization": "Bearer " + token}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            docs = await client.get("/api/openapi.json")
            assert docs.status_code == 200
            document = docs.json()
            if not configured:
                assert service is None
                assert not hasattr(app.state, "canonical_organization_login_policy")
                assert url not in document["paths"]
                for verb, kwargs in [
                    ("PUT", {"json": {"login_method": "password"}}),
                    ("DELETE", {}),
                ]:
                    response = await client.request(verb, url, headers=headers, **kwargs)
                    assert response.status_code == 503
                    assert response.json() == {"canonical_resource_unavailable": True}
                assert _stored_login_policy(environment.path) is None
                return
            assert app.state.canonical_organization_login_policy is service
            assert service._snapshots is app.state.canonical_identity is bound.business.snapshots
            assert service._readiness is readiness
            operations = document["paths"][url]
            assert set(operations) == {"put", "delete"}
            assert all(operation["tags"] == ["auth"] for operation in operations.values())
            schema = document["components"]["schemas"]["OrganizationLoginMethodBody"]
            assert schema["properties"]["login_method"]["enum"] == ["sms", "password", "cas"]
            assert schema["additionalProperties"] is False
            before_rejections = environment.writer.calls
            for credentials in [[], [("Authorization", "Bearer " + token)] * 2]:
                denied = await client.put(
                    url, json={"login_method": "password"}, headers=credentials
                )
                assert denied.status_code == 401
                assert denied.headers["cache-control"] == "no-store"
            invalid = await client.put(
                url,
                json={"login_method": "password", "access_token": "private-body-marker"},
                headers=headers,
            )
            assert invalid.status_code == 422 and "private-body-marker" not in invalid.text
            assert invalid.headers["cache-control"] == "no-store"
            assert (
                await client.put(
                    url + "?scope=platform", json={"login_method": "password"}, headers=headers
                )
            ).status_code == 400
            assert (
                await client.request("DELETE", url, json={"scope": "platform"}, headers=headers)
            ).status_code == 400
            assert environment.writer.calls == before_rejections
            assert _stored_login_policy(environment.path) is None
            result = await client.put(url, json={"login_method": "password"}, headers=headers)
            assert result.status_code == 200 and result.json() == {"success": True}
            assert result.headers["cache-control"] == "no-store"
            stored = _stored_login_policy(environment.path)
            assert stored[0]["loginMethod"] == 1 and stored[1] == _IDS["homeMember"]
            readiness.result = False
            denied = await client.put(url, json={"login_method": "sms"}, headers=headers)
            assert denied.status_code == 503 and _stored_login_policy(environment.path) == stored
            assert readiness.calls == [("sms", _IDS["homeOrg"])]
            inherited = await client.delete(url, headers=headers)
            assert inherited.status_code == 200 and inherited.json() == {"success": True}
            assert _stored_login_policy(environment.path)[0] == {"loginMethodInherited": True}
            writer_calls = environment.writer.calls
            for verb, path in [
                ("GET", url),
                ("POST", url),
                ("PATCH", url),
                ("PUT", url + "/child"),
            ]:
                response = await client.request(verb, path, headers=headers)
                assert response.status_code == 503
                assert response.json() == {"canonical_resource_unavailable": True}
            assert environment.writer.calls == writer_calls

    await _policy_foreground(selected, isolated_boot, monkeypatch, exercise)


@pytest.mark.parametrize(
    "replacement", ["missing", "other-service", "wrong-type", "snapshot", "configured"]
)
async def test_policy_complete_middleware_refuses_inconsistent_configured_instance(
    configuration, isolated_boot, environment, monkeypatch, replacement
):
    from octop.infra.identity.organization_login_policy import (
        CanonicalOrganizationLoginPolicyService,
    )
    from tests.unit.identity.test_organization_login_policy import _Readiness

    selected = replace(configuration, login_policy_readiness=_Readiness())

    async def exercise(app, server):
        bound = server.canonical
        service = bound.business.organization_login_policy
        token = await _policy_administrator(environment, bound.business)
        other = CanonicalOrganizationLoginPolicyService(
            environment.path,
            service._snapshots,
            writer=SimpleNamespace(assert_current_writer=AsyncMock()),
            readiness=_Readiness(),
            clock=configuration.clock,
        )
        if replacement == "missing":
            app.state.canonical_organization_login_policy = None
        elif replacement == "other-service":
            app.state.canonical_organization_login_policy = other
        elif replacement == "wrong-type":
            app.state.canonical_organization_login_policy = object()
        elif replacement == "snapshot":
            app.state.canonical_identity = object()
        else:
            server.canonical = replace(
                bound, business=replace(bound.business, organization_login_policy=other)
            )
        before = environment.writer.calls
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for verb, kwargs in [
                    ("PUT", {"json": {"login_method": "password"}}),
                    ("DELETE", {}),
                ]:
                    response = await client.request(
                        verb,
                        "/api/identity/organization/login-policy",
                        headers={"Authorization": "Bearer " + token},
                        **kwargs,
                    )
                    assert response.status_code == 503
                    assert response.json() == {"canonical_resource_unavailable": True}
            assert environment.writer.calls == before
            assert _stored_login_policy(environment.path) is None
        finally:
            server.canonical = bound

    await _policy_foreground(selected, isolated_boot, monkeypatch, exercise)


async def test_policy_foreground_postcommit_withdrawal_withholds_ack_without_replay(
    configuration, isolated_boot, environment, monkeypatch
):
    from tests.unit.identity.test_human_sessions import _IDS
    from tests.unit.identity.test_organization_login_policy import _Readiness

    selected = replace(configuration, login_policy_readiness=_Readiness())

    async def exercise(app, server):
        token = await _policy_administrator(environment, server.canonical.business)
        before = environment.writer.calls

        def withdraw(calls):
            if calls == before + 3:
                app.state.canonical_organization_login_policy = None

        environment.writer.on_call = withdraw
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                "/api/identity/organization/login-policy",
                json={"login_method": "password"},
                headers={"Authorization": "Bearer " + token},
            )
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert environment.writer.calls == before + 3
        stored = _stored_login_policy(environment.path)
        assert stored[0]["loginMethod"] == 1 and stored[1] == _IDS["homeMember"]

    await _policy_foreground(selected, isolated_boot, monkeypatch, exercise)


def _stored_platform_policy(path):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT policy_json,updated_by,created_at,updated_at FROM client_delivery_policies "
            "WHERE scope_type='platform' AND scope_id='default'"
        ).fetchone()


def _stored_organization_policies(path):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT * FROM client_delivery_policies WHERE scope_type='organization' "
            "ORDER BY scope_id"
        ).fetchall()


async def _platform_actor(environment, business, role="super_admin"):
    from tests.unit.identity.test_canonical_business_composition import _login
    from tests.unit.identity.test_human_sessions import _IDS

    with sqlite3.connect(environment.path) as db:
        db.execute("UPDATE users SET role=? WHERE id=?", (role, _IDS["homeMember"]))
    return await _login(environment, business)


@pytest.mark.parametrize("configured", [False, True])
async def test_platform_policy_one_foreground_mount_readiness_and_organization_isolation(
    configuration, isolated_boot, environment, monkeypatch, configured
):
    from fastapi.routing import iter_route_contexts

    from tests.unit.identity.test_human_sessions import _IDS
    from tests.unit.identity.test_organization_login_policy import _Readiness as OrgReadiness
    from tests.unit.identity.test_platform_login_policy import _Readiness as PlatformReadiness

    platform_readiness, org_readiness = PlatformReadiness(), OrgReadiness()
    selected = replace(
        configuration,
        login_policy_readiness=org_readiness,
        platform_login_policy_readiness=platform_readiness if configured else None,
    )

    async def exercise(app, server):
        url = "/api/identity/platform/login-policy"
        org_url = "/api/identity/organization/login-policy"
        business = server.canonical.business
        service = business.platform_login_policy
        assert business.organization_login_policy._readiness is org_readiness
        assert len(business.app_kwargs()) == 11
        assert set(vars(server.canonical.runtime_inputs)) == {
            "business",
            "services",
            "originals",
            "selections",
            "execution",
        }
        assert len([route for route in iter_route_contexts(app.routes) if route.path == url]) == (
            1 if configured else 0
        )
        assert (
            len([route for route in iter_route_contexts(app.routes) if route.path == org_url]) == 2
        )
        assert platform_readiness.calls == org_readiness.calls == []
        with sqlite3.connect(environment.path) as db:
            db.execute(
                "INSERT INTO client_delivery_policies VALUES ('platform','default',?,?,7,8)",
                ('{"loginMethod":0,"keep":4}', "original-platform-writer"),
            )
            db.execute(
                "INSERT INTO client_delivery_policies VALUES ('organization',?,?,?,3,4)",
                (_IDS["homeOrg"], '{"loginMethod":0,"keepOrg":9}', "original-org-writer"),
            )
        organizations = _stored_organization_policies(environment.path)
        platform_before = _stored_platform_policy(environment.path)
        token = (await _platform_actor(environment, business)).access_token
        headers = {"Authorization": "Bearer " + token}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            docs = await client.get("/api/openapi.json")
            assert docs.status_code == 200
            document = docs.json()
            assert set(document["paths"][org_url]) == {"put", "delete"}
            if not configured:
                assert service is None
                assert not hasattr(app.state, "canonical_platform_login_policy")
                assert url not in document["paths"]
                before = environment.writer.calls
                response = await client.put(url, json={"login_method": "password"}, headers=headers)
                assert response.status_code == 503
                assert response.json() == {"canonical_resource_unavailable": True}
                assert environment.writer.calls == before
                assert _stored_platform_policy(environment.path) == platform_before
                assert _stored_organization_policies(environment.path) == organizations
                return
            assert app.state.canonical_platform_login_policy is service
            assert service._snapshots is business.snapshots is app.state.canonical_identity
            assert service._readiness is platform_readiness
            assert service._writer is selected.writer and service._clock is selected.clock
            assert set(document["paths"][url]) == {"put"}
            assert document["paths"][url]["put"]["tags"] == ["auth"]
            body = document["components"]["schemas"]["PlatformLoginMethodBody"]
            assert body["additionalProperties"] is False
            assert body["properties"]["login_method"]["enum"] == ["sms", "password", "cas"]
            before = environment.writer.calls
            for credentials in [[], [("Authorization", "Bearer " + token)] * 2]:
                denied = await client.put(
                    url, json={"login_method": "password"}, headers=credentials
                )
                assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
            invalid = await client.put(
                url,
                json={"login_method": "password", "access_token": "private-platform-marker"},
                headers=headers,
            )
            assert invalid.status_code == 422 and "private-platform-marker" not in invalid.text
            assert (
                await client.put(
                    url + "?organization_id=other",
                    json={"login_method": "password"},
                    headers=headers,
                )
            ).status_code == 400
            assert environment.writer.calls == before
            result = await client.put(url, json={"login_method": "password"}, headers=headers)
            assert result.status_code == 200 and result.json() == {"success": True}
            assert result.headers["cache-control"] == "no-store"
            stored = _stored_platform_policy(environment.path)
            assert json.loads(stored[0]) == {"loginMethod": 1, "keep": 4}
            assert stored[1] == _IDS["homeMember"] and stored[2] == 7
            assert _stored_organization_policies(environment.path) == organizations
            platform_readiness.result = False
            unavailable = await client.put(url, json={"login_method": "sms"}, headers=headers)
            assert unavailable.status_code == 503
            assert _stored_platform_policy(environment.path) == stored
            assert platform_readiness.calls == ["sms"] and org_readiness.calls == []
            platform_readiness.result = None
            cas = await client.put(url, json={"login_method": "cas"}, headers=headers)
            assert cas.status_code == 200
            assert json.loads(_stored_platform_policy(environment.path)[0]) == {
                "loginMethod": 2,
                "keep": 4,
            }
            assert platform_readiness.calls == ["sms", "cas"] and org_readiness.calls == []
            assert _stored_organization_policies(environment.path) == organizations
            before = environment.writer.calls
            for verb, path in [
                ("GET", url),
                ("POST", url),
                ("PATCH", url),
                ("DELETE", url),
                ("PUT", url + "/child"),
            ]:
                response = await client.request(verb, path, headers=headers)
                assert response.status_code == 503
                assert response.json() == {"canonical_resource_unavailable": True}
            assert environment.writer.calls == before

    await _policy_foreground(selected, isolated_boot, monkeypatch, exercise)


@pytest.mark.parametrize(
    "kind", ["false", "object", "function", "org-only", "noncallable-platform"]
)
def test_platform_readiness_refuses_wrong_scope_before_pool_open(configuration, monkeypatch, kind):
    candidates = {
        "false": False,
        "object": object(),
        "function": lambda: None,
        "org-only": SimpleNamespace(assert_available=AsyncMock()),
        "noncallable-platform": SimpleNamespace(assert_platform_available=False),
    }
    opened = Mock(side_effect=AssertionError("Product pool must not open"))
    monkeypatch.setattr("octop.infra.server.open_database", opened)
    before = digest(configuration.database_path)
    with pytest.raises(TypeError, match="platform login-method readiness"):
        replace(configuration, platform_login_policy_readiness=candidates[kind])
    opened.assert_not_called()
    assert configuration.backend_factory.inputs == []
    assert digest(configuration.database_path) == before


@pytest.mark.parametrize(
    "actor", ["organization-admin", "limited-super", "demoted-super", "admin-ceiling-super"]
)
async def test_platform_policy_middleware_requires_current_super_and_signed_ceiling(
    configuration, isolated_boot, environment, monkeypatch, actor
):
    from tests.unit.identity.test_canonical_organization_members_port import _signed
    from tests.unit.identity.test_human_sessions import _IDS
    from tests.unit.identity.test_platform_login_policy import _Readiness

    selected = replace(configuration, platform_login_policy_readiness=_Readiness())

    async def exercise(app, server):
        role = "admin" if actor == "organization-admin" else "super_admin"
        tokens = await _platform_actor(environment, server.canonical.business, role)
        token = tokens.access_token
        if actor == "limited-super":
            token = _signed(tokens, scopes=["admin:users"])
        elif actor == "admin-ceiling-super":
            token = _signed(tokens, scopes=["admin:*"])
        elif actor == "demoted-super":
            with sqlite3.connect(environment.path) as db:
                db.execute("UPDATE users SET role='admin' WHERE id=?", (_IDS["homeMember"],))
        before = environment.writer.calls
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                "/api/identity/platform/login-policy",
                json={"login_method": "password"},
                headers={"Authorization": "Bearer " + token},
            )
        expected = {
            "organization-admin": 403,
            "limited-super": 403,
            "demoted-super": 401,
            "admin-ceiling-super": 200,
        }[actor]
        assert response.status_code == expected
        assert response.headers["cache-control"] == "no-store"
        if actor != "admin-ceiling-super":
            assert environment.writer.calls == before
            assert _stored_platform_policy(environment.path) is None
        else:
            assert json.loads(_stored_platform_policy(environment.path)[0])["loginMethod"] == 1

    await _policy_foreground(selected, isolated_boot, monkeypatch, exercise)


@pytest.mark.parametrize(
    "replacement", ["missing", "other-service", "wrong-type", "snapshot", "configured"]
)
async def test_platform_policy_middleware_refuses_replaced_exact_binding(
    configuration, isolated_boot, environment, monkeypatch, replacement
):
    from octop.infra.identity.platform_login_policy import CanonicalPlatformLoginPolicyService
    from tests.unit.identity.test_platform_login_policy import _Readiness

    selected = replace(configuration, platform_login_policy_readiness=_Readiness())

    async def exercise(app, server):
        bound = server.canonical
        service = bound.business.platform_login_policy
        token = (await _platform_actor(environment, bound.business)).access_token
        other = CanonicalPlatformLoginPolicyService(
            environment.path,
            service._snapshots,
            writer=SimpleNamespace(assert_current_writer=AsyncMock()),
            readiness=_Readiness(),
            clock=configuration.clock,
        )
        if replacement == "missing":
            app.state.canonical_platform_login_policy = None
        elif replacement == "other-service":
            app.state.canonical_platform_login_policy = other
        elif replacement == "wrong-type":
            app.state.canonical_platform_login_policy = object()
        elif replacement == "snapshot":
            app.state.canonical_identity = object()
        else:
            server.canonical = replace(
                bound, business=replace(bound.business, platform_login_policy=other)
            )
        before = environment.writer.calls
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.put(
                    "/api/identity/platform/login-policy",
                    json={"login_method": "password"},
                    headers={"Authorization": "Bearer " + token},
                )
            assert response.status_code == 503
            assert response.json() == {"canonical_resource_unavailable": True}
            assert environment.writer.calls == before
            assert _stored_platform_policy(environment.path) is None
        finally:
            server.canonical = bound

    await _policy_foreground(selected, isolated_boot, monkeypatch, exercise)


async def test_platform_policy_postcommit_withdrawal_preserves_sql_without_ack_or_replay(
    configuration, isolated_boot, environment, monkeypatch
):
    from tests.unit.identity.test_human_sessions import _IDS
    from tests.unit.identity.test_platform_login_policy import _Readiness

    selected = replace(configuration, platform_login_policy_readiness=_Readiness())

    async def exercise(app, server):
        token = (await _platform_actor(environment, server.canonical.business)).access_token
        organizations = _stored_organization_policies(environment.path)
        before = environment.writer.calls

        def withdraw(calls):
            if calls == before + 3:
                app.state.canonical_platform_login_policy = None

        environment.writer.on_call = withdraw
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                "/api/identity/platform/login-policy",
                json={"login_method": "password"},
                headers={"Authorization": "Bearer " + token},
            )
        assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
        assert environment.writer.calls == before + 3
        row = _stored_platform_policy(environment.path)
        assert json.loads(row[0])["loginMethod"] == 1 and row[1] == _IDS["homeMember"]
        assert _stored_organization_policies(environment.path) == organizations

    await _policy_foreground(selected, isolated_boot, monkeypatch, exercise)


async def test_canonical_boot_does_not_seed_an_unavailable_standalone_market(
    configuration, isolated_boot, monkeypatch
):
    seed = Mock(
        side_effect=AssertionError("Standalone market data is unavailable in canonical mode")
    )
    monkeypatch.setattr("octop.infra.market.skillhub_defaults.seed_skillhub_market", seed)
    before = digest(configuration.database_path)
    server = MossworkServer(isolated_boot, canonical_configuration=configuration)
    await server.start()
    try:
        bound = server.canonical
        assert bound is not None
        app = build_app(
            server, **bound.business.app_kwargs(), canonical_admissions=bound.admissions
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/identity/status")).json()["mode"] == "canonical"
            unavailable = await client.get("/api/market")
            assert unavailable.status_code == 503
            assert unavailable.json() == {"canonical_resource_unavailable": True}
        seed.assert_not_called()
        assert digest(configuration.database_path) == before
    finally:
        await server.stop()


async def test_standalone_boot_retains_its_market_seed_path(isolated_boot, monkeypatch):
    isolated_boot.mkdir()
    product_db = isolated_boot / "product.db"
    sqlite3.connect(product_db).close()
    (isolated_boot / "config.json").write_text(
        json.dumps({"database": {"driver": "sqlite", "sqlite_path": str(product_db)}}),
        encoding="utf-8",
    )
    original = RuntimeError("Standalone market seed reached")
    seed = Mock(side_effect=original)
    monkeypatch.setattr("octop.infra.market.skillhub_defaults.seed_skillhub_market", seed)
    server = MossworkServer(isolated_boot)
    try:
        with pytest.raises(RuntimeError) as failure:
            await server.start()
        assert failure.value is original
        seed.assert_called_once()
        assert server.canonical is None
    finally:
        await server.stop()
        if server.services is not None:
            server.services.db.close()
        await server.management_io.drain()
