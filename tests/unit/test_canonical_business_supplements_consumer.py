"""Real ASGI/business/queue composition; native backend and SMS remain fixtures."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from octop.api.app import build_app
from octop.config import MossworkConfig
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.execution.admission import CanonicalRunAdmissions
from octop.infra.execution.service import ExecutionService
from octop.infra.identity.composition import build_canonical_business
from octop.infra.identity.request_principal import authenticate
from octop.infra.server import MossworkServer
from tests.unit.agents.test_managed_execution_backend import FixtureBackend
from tests.unit.identity.test_canonical_business_composition import _PASSWORD
from tests.unit.identity.test_canonical_business_composition import environment as environment
from tests.unit.identity.test_canonical_business_composition import legacy_db as legacy_db
from tests.unit.identity.test_canonical_business_composition import product as product
from tests.unit.identity.test_human_sessions import _IDS, _NUMBER


@pytest.fixture
async def managed_business(environment, tmp_path):
    # Only fixture DDL. The production constructor must not create this table.
    fixture = json.loads(
        (Path(__file__).parent / "identity/fixtures/self_profile_canonical_legacy.json").read_text(
            encoding="utf-8"
        )
    )
    avatar_ddl = fixture["extension"].split("CREATE TABLE user_profile_avatars", 1)[1]
    with sqlite3.connect(environment.path) as db:
        db.executescript("CREATE TABLE user_profile_avatars" + avatar_ddl)
    enabled = [True]
    bundle = build_canonical_business(
        environment.path, **{**environment.kwargs, "phone_enabled": lambda: enabled[0]}
    )
    backend = FixtureBackend()
    server = MossworkServer(tmp_path / "managed-home", managed_backend=backend)
    cfg = MossworkConfig(enable_api_docs=True, enable_dashboard=False)
    server.config = cfg
    server.services = build_shared_services(db=environment.product, paths=server.paths, config=cfg)
    repo = server.services.execution_repo
    gate = CanonicalRunAdmissions(repo)
    service = ExecutionService(
        repo, node="fixture-web", incarnation="fixture-boot", role="web", human_admissions=gate
    )
    server.execution, server._started = service, True
    app = build_app(server, **bundle.app_kwargs(), canonical_admissions=gate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            app=app,
            server=server,
            bundle=bundle,
            gate=gate,
            repo=repo,
            client=client,
            backend=backend,
            enabled=enabled,
            original=environment,
        )
    await service.io.drain()
    await server.management_io.drain()


async def test_profile_phone_disable_and_same_unsent_http_reauthentication_share_one_bundle(
    managed_business,
):
    env = managed_business
    client = env.client
    refused = await client.get("/api/identity/profile")
    assert refused.status_code == 401 and refused.headers["cache-control"] == "no-store"
    sent = await client.post("/api/identity/phone/send-code", json={"phone": _NUMBER})
    assert sent.status_code == 200
    login = await client.post(
        "/api/identity/phone/login",
        json={"phone": _NUMBER, "code": env.original.sender.sent[-1][1]},
    )
    assert login.status_code == 200
    pair = login.json()
    headers = {"Authorization": "Bearer " + pair["access_token"]}
    profile = await client.get("/api/identity/profile", headers=headers)
    assert profile.status_code == 200 and profile.json()["member_id"] == _IDS["homeMember"]
    assert profile.headers["cache-control"] == "no-store"
    assert UserRepo(env.original.product).count() == 0
    updated = await client.post(
        "/api/identity/profile/nickname", headers=headers, json={"nickname": "Original member"}
    )
    assert updated.status_code == 200
    assert (await client.get("/api/identity/profile", headers=headers)).json()[
        "nickname"
    ] == "Original member"
    principal = await authenticate(env.bundle.requests, pair["access_token"])
    user = principal.identity.projection.local_user_id
    AgentRepo(env.original.product).create(agent_id="original-agent", user_id=user, name="Original")
    ThreadRepo(env.original.product).insert(
        thread_id="original-thread",
        agent_id="original-agent",
        user_id=user,
        channel_type="dashboard",
        session_key="original-session-reference",
    )
    # An earlier process's sealed admission is absent from the current gate.
    original_gate = CanonicalRunAdmissions(env.repo)
    run = await original_gate.submit(
        principal,
        user=user,
        agent="original-agent",
        thread="original-thread",
        operation="chat",
        key="same-original-operation",
        request={"request": {"text": "original request"}},
    )
    before = env.repo.get(run["run_id"])
    refresh = await client.post(
        "/api/identity/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert refresh.status_code == 200
    fresh = refresh.json()
    env.enabled[0] = False
    assert (await client.get("/api/identity/status")).json()["phone_login_available"] is False
    assert (
        await client.post("/api/identity/phone/send-code", json={"phone": _NUMBER})
    ).status_code == 404
    recovered = await client.post(
        f"/api/executions/{run['run_id']}/reauthenticate",
        headers={"Authorization": "Bearer " + fresh["access_token"]},
    )
    assert recovered.status_code == 204 and recovered.headers["cache-control"] == "no-store"
    assert env.repo.get(run["run_id"]) == before
    held = await env.gate.current(before)
    assert held.identity.projection == principal.identity.projection
    assert held.identity.snapshot.credential_id != principal.identity.snapshot.credential_id
    assert UserRepo(env.original.product).count() == 1
    assert fresh["access_token"] not in before["request_json"]
    assert pair["access_token"] not in before["request_json"]
    env.backend.call.assert_not_awaited()
    assert env.backend.streams == [] and env.original.native.calls == []


@pytest.mark.parametrize("setting,expected", [(False, 404), (None, 503), ("false", 503)])
async def test_effective_setting_is_current_and_cannot_select_another_auth_mode(
    managed_business, setting, expected
):
    env = managed_business
    env.enabled[0] = setting
    status = await env.client.get("/api/identity/status")
    assert status.status_code == (200 if setting is False else 503)
    assert status.headers["cache-control"] == "no-store"
    if setting is False:
        assert status.json()["mode"] == "canonical"
        assert status.json()["password_login_available"] is True
    sent = await env.client.post("/api/identity/phone/send-code", json={"phone": _NUMBER})
    assert sent.status_code == expected and sent.headers["cache-control"] == "no-store"
    assert UserRepo(env.original.product).count() == 0
    assert env.original.writer.calls == 0 and env.original.sender.sent == []
    assert env.original.native.calls == []


async def test_disabled_sms_without_ports_keeps_real_password_http_issuer(managed_business):
    env = managed_business
    bundle = build_canonical_business(
        env.original.path,
        **{
            **env.original.kwargs,
            "phone_enabled": lambda: False,
            "phone_config": None,
            "sms_sender": None,
        },
    )
    with sqlite3.connect(env.original.path) as db:
        db.execute(
            "INSERT OR REPLACE INTO client_delivery_policies VALUES "
            "('platform','default',?,'fixture',1,1)",
            ('{"loginMethod":"password"}',),
        )
        db.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (_PASSWORD["passwordHash"], _IDS["homeMember"]),
        )
    app = build_app(env.server, **bundle.app_kwargs(), canonical_admissions=env.gate)
    assert bundle.phone is None and bundle.password._issuer is bundle.sessions
    assert bundle.api_key_exchange._issuer is bundle.sessions
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/identity/password/login",
            json={"username": "home", "password": _PASSWORD["password"]},
        )
        assert response.status_code == 200
        pair = response.json()
        profile = await client.get(
            "/api/identity/profile", headers={"Authorization": "Bearer " + pair["access_token"]}
        )
        assert profile.status_code == 200 and profile.json()["member_id"] == _IDS["homeMember"]
        refresh = await client.post(
            "/api/identity/refresh", json={"refresh_token": pair["refresh_token"]}
        )
        assert refresh.status_code == 200
        assert (await bundle.snapshots.snapshot(refresh.json()["access_token"])).key_id == (
            "password-login"
        )
    assert UserRepo(env.original.product).count() == 0
    assert env.original.sender.sent == [] and env.original.native.calls == []


def test_profile_cannot_mix_equal_authority_other_snapshot_with_queue(managed_business):
    env = managed_business
    other = build_canonical_business(env.original.path, **env.original.kwargs)
    assert other.snapshots is not env.bundle.snapshots
    with pytest.raises(ValueError, match="same trusted authority"):
        build_app(
            env.server,
            **{**env.bundle.app_kwargs(), "canonical_profile": other.profile},
            canonical_admissions=env.gate,
        )
