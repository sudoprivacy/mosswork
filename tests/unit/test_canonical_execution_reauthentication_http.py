"""Real HTTP/human issuer/queue recovery, with no native dispatch or grants."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from octop.api.app import build_app
from octop.config import MossworkConfig
from octop.infra.auth.tokens import sign_token
from octop.infra.db.repos.execution import ExecutionRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.execution.admission import CanonicalRunAdmissions
from octop.infra.execution.service import ExecutionService
from octop.infra.server import MossworkServer
from tests.unit.agents.test_managed_execution_backend import FixtureBackend
from tests.unit.identity.test_human_sessions import _IDS, _SECRET
from tests.unit.test_canonical_queue_reauthentication import legacy_db as legacy_db
from tests.unit.test_canonical_queue_reauthentication import original_queue as original_queue
from tests.unit.test_canonical_queue_reauthentication import product as product
from tests.unit.test_canonical_queue_reauthentication import sessions as sessions


@pytest.fixture
async def recovery(original_queue, sessions, tmp_path):
    case = original_queue
    backend = FixtureBackend()
    server = MossworkServer(tmp_path / "app-home", managed_backend=backend)
    cfg = MossworkConfig(enable_api_docs=True, enable_dashboard=False)
    server.config = cfg
    server.services = build_shared_services(db=case.product, paths=server.paths, config=cfg)
    # A restarted process has a new repo object over the same Product database.
    # Install exactly the service's repository and a new empty private carrier.
    case.repo = server.services.execution_repo
    case.gate = CanonicalRunAdmissions(case.repo)
    service = ExecutionService(
        case.repo,
        node="recovery-api",
        incarnation="recovery-api-boot",
        role="web",
        human_admissions=case.gate,
    )
    server.execution = service
    server._started = True
    app = build_app(
        server,
        canonical_sessions=sessions.service,
        canonical_requests=case.resolver,
        canonical_admissions=case.gate,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            case=case,
            sessions=sessions,
            server=server,
            backend=backend,
            service=service,
            app=app,
            client=client,
            path=f"/api/executions/{case.run['run_id']}/reauthenticate",
            headers={"Authorization": "Bearer " + case.fresh.access_token},
        )
    await service.io.drain()
    await server.management_io.drain()


async def test_fresh_human_http_rebinds_same_unsent_request_without_dispatch(recovery):
    env, case = recovery, recovery.case
    before = case.repo.get(case.run["run_id"])
    response = await env.client.post(
        env.path,
        headers=env.headers,
        json={"canonical_actor": {"role": "admin"}, "run_id": "other", "local_user_id": 9999},
    )
    assert response.status_code == 204 and response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert "x-mosswork-access-token" not in response.headers
    assert case.repo.get(case.run["run_id"]) == before
    current = await case.gate.current(before)
    assert current.identity.projection == case.principal.identity.projection
    assert current.identity.snapshot.credential_id != case.principal.identity.snapshot.credential_id
    assert current.identity.projection.local_user_id != before["user_id"]
    assert UserRepo(case.product).count() == 2
    env.backend.call.assert_not_awaited()
    assert env.backend.streams == []


async def test_recovery_docs_and_adjacent_operations_keep_distinct_auth(recovery):
    env = recovery
    spec = (await env.client.get("/api/openapi.json")).json()
    operation = spec["paths"]["/api/executions/{run_id}/reauthenticate"]["post"]
    assert operation["security"] == [{"CanonicalRequestBearer": []}]
    assert operation["summary"] and "204" in operation["responses"]
    assert "requestBody" not in operation
    assert (await env.client.get("/api/docs")).status_code == 200
    for path, method in [
        (env.path, "get"),
        (env.path.replace("/reauthenticate", ""), "get"),
        (env.path.replace("/reauthenticate", "/cancel"), "post"),
    ]:
        # The one adapted route must not exempt neighboring integer-user APIs.
        response = await env.client.request(method, path, headers=env.headers)
        assert response.status_code == 503


@pytest.mark.parametrize("channel", ["missing", "query", "body", "duplicate", "refresh", "local"])
async def test_recovery_accepts_only_one_current_human_bearer(recovery, channel):
    env, case = recovery, recovery.case
    path, headers, body = env.path, {}, None
    if channel == "query":
        path += "?access_token=" + case.fresh.access_token
    elif channel == "body":
        body = {"token": case.fresh.access_token, "canonical_actor": {"role": "admin"}}
    elif channel == "duplicate":
        headers = [("Authorization", env.headers["Authorization"])] * 2
    elif channel == "refresh":
        headers = {"Authorization": "Bearer " + case.fresh.refresh_token}
    elif channel == "local":
        headers = {
            "Authorization": "Bearer " + sign_token(_SECRET, sub=1, uname="admin", role="admin")
        }
    before = case.repo.get(case.run["run_id"])
    response = await env.client.post(path, headers=headers, json=body)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert case.repo.get(case.run["run_id"]) == before
    assert await case.gate.claim("same-worker", "same-boot") is None


async def test_other_current_organization_cannot_rebind_original_actor(recovery):
    env, case = recovery, recovery.case
    other = await env.sessions.service.switch_organization(
        case.fresh.access_token, _IDS["companyOrg"]
    )
    before = case.repo.get(case.run["run_id"])
    response = await env.client.post(
        env.path, headers={"Authorization": "Bearer " + other.access_token}
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert case.repo.get(case.run["run_id"]) == before
    assert await case.gate.claim("same-worker", "same-boot") is None
    assert UserRepo(case.product).count() == 2


async def test_missing_projection_cannot_allocate_on_http_recovery(recovery):
    env, case = recovery, recovery.case
    with case.product.transaction() as connection:
        connection.execute("DELETE FROM canonical_user_projections")
    response = await env.client.post(env.path, headers=env.headers)
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert UserRepo(case.product).count() == 2
    assert await case.gate.claim("same-worker", "same-boot") is None
    with case.product.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM canonical_user_projections").fetchone()[0] == 0
        )


async def test_logged_out_human_cannot_recover_the_queued_request(recovery):
    env, case = recovery, recovery.case
    await env.sessions.service.logout(case.fresh.access_token, case.fresh.refresh_token)
    before = case.repo.get(case.run["run_id"])
    response = await env.client.post(env.path, headers=env.headers)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert case.repo.get(case.run["run_id"]) == before
    assert await case.gate.claim("same-worker", "same-boot") is None


@pytest.mark.parametrize("state", ["running", "unknown", "awaiting_approval", "completed"])
async def test_http_reauthentication_does_not_replay_submitted_work(recovery, state):
    env, case = recovery, recovery.case
    claimed = await case.previous.claim("same-worker", "same-boot")
    if state != "running":
        case.repo.transition(
            claimed["run_id"],
            "same-worker",
            "same-boot",
            claimed["token"],
            state,
            approval_id="original-approval" if state == "awaiting_approval" else None,
        )
    before = case.repo.get(claimed["run_id"])
    response = await env.client.post(env.path, headers=env.headers)
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert case.repo.get(claimed["run_id"]) == before
    assert await case.gate.claim("same-worker", "same-boot") is None
    env.backend.call.assert_not_awaited()


@pytest.mark.parametrize("changed", ["carrier", "repo", "backend"])
async def test_receiver_rejects_a_different_or_missing_managed_queue(recovery, changed):
    env, case = recovery, recovery.case
    if changed == "carrier":
        env.service.human_admissions = CanonicalRunAdmissions(case.repo)
    elif changed == "repo":
        env.service.repo = ExecutionRepo(case.product)
    else:
        env.server._managed_backend = None
    with pytest.raises(ValueError, match="same configured managed queue"):
        build_app(
            env.server,
            canonical_sessions=env.sessions.service,
            canonical_requests=case.resolver,
            canonical_admissions=case.gate,
        )
    response = await env.client.post(env.path, headers=env.headers)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert await case.gate.claim("same-worker", "same-boot") is None


async def test_private_verifier_error_is_not_an_http_success_or_diagnostic(recovery, monkeypatch):
    env, case = recovery, recovery.case

    async def unavailable(token):
        raise RuntimeError("PRIVATE_VERIFIER_DIAGNOSTIC")

    monkeypatch.setattr(case.resolver._snapshots, "snapshot", unavailable)
    response = await env.client.post(env.path, headers=env.headers)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert "PRIVATE_VERIFIER_DIAGNOSTIC" not in response.text
    assert case.fresh.access_token not in response.text
    assert await case.gate.claim("same-worker", "same-boot") is None


async def test_http_task_cancellation_does_not_bind_or_return_success(recovery, monkeypatch):
    env, case = recovery, recovery.case
    entered = asyncio.Event()

    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(case.resolver, "reauthenticate", blocked)
    before = case.repo.get(case.run["run_id"])
    task = asyncio.create_task(env.client.post(env.path, headers=env.headers))
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert case.repo.get(case.run["run_id"]) == before
    assert await case.gate.claim("same-worker", "same-boot") is None
