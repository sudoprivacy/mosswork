"""Real canonical JWT/SQLite and durable queue; native/provider effects are fixtures."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from octop.config import MossworkConfig
from octop.infra.agents.manager import AgentManager
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.execution import ExecutionRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.errors import MossworkError
from octop.infra.execution.admission import CanonicalRunAdmissions, CurrentAdmissionRequired
from octop.infra.execution.gateway import GatewayExecution
from octop.infra.execution.service import ExecutionService, current_execution
from octop.infra.identity.request_principal import authenticate
from tests.unit.agents.test_agent_manager import manager as manager
from tests.unit.agents.test_managed_execution_backend import FixtureBackend
from tests.unit.identity.test_canonical_human_request_projection import (
    _resolver,
    _token,
)
from tests.unit.identity.test_canonical_human_request_projection import legacy_db as legacy_db
from tests.unit.identity.test_canonical_human_request_projection import product as product


@pytest.fixture
async def admitted(legacy_db, product):
    resolver = _resolver(legacy_db, product)
    principal = await authenticate(resolver, _token())
    user = principal.identity.projection.local_user_id
    AgentRepo(product).create(agent_id="expert", user_id=user, name="Fixture resource")
    for thread in ("one", "two"):
        ThreadRepo(product).insert(
            thread_id=thread,
            agent_id="expert",
            user_id=user,
            channel_type="dashboard",
            session_key=thread,
        )
    repo = ExecutionRepo(product)
    repo.register("worker", "boot1", "worker")
    gate = CanonicalRunAdmissions(repo)
    return repo, gate, principal, resolver, user


async def submit(admitted, *, thread="one", key="original-request"):
    repo, gate, principal, _resolver, user = admitted
    return await gate.submit(
        principal,
        user=user,
        agent="expert",
        thread=thread,
        operation="chat",
        key=key,
        request={"mode": "harness", "request": {"text": "original message"}},
    )


async def test_restart_requires_fresh_auth_for_same_original_unsent_request(admitted):
    repo, gate, principal, resolver, _user = admitted
    run = await submit(admitted)
    persisted = repo.get(run["run_id"])["request_json"]
    assert _token() not in persisted
    payload = json.loads(persisted)
    assert set(payload["canonical_actor"]) == {
        "authority_id",
        "account_id",
        "organization_id",
        "member_id",
        "local_user_id",
    }
    assert "scopes" not in persisted and "role" not in payload["canonical_actor"]
    restarted = CanonicalRunAdmissions(repo)
    assert await restarted.claim("worker", "boot1") is None
    assert repo.get(run["run_id"])["state"] == "queued"
    fresh = await authenticate(resolver, _token())
    assert fresh is not principal
    await restarted.reauthenticate(run["run_id"], fresh)
    claimed = await restarted.claim("worker", "boot1")
    assert claimed["run_id"] == run["run_id"]
    assert claimed["request_json"] == persisted
    assert await restarted.current(claimed) is fresh
    assert gate.repo is restarted.repo


@pytest.mark.parametrize("state", ["running", "unknown", "awaiting_approval"])
async def test_submitted_or_unknown_cannot_be_reauthenticated_as_new(admitted, state):
    repo, gate, _principal, resolver, _user = admitted
    run = await submit(admitted)
    claimed = await gate.claim("worker", "boot1")
    if state != "running":
        repo.transition(
            claimed["run_id"],
            "worker",
            "boot1",
            claimed["token"],
            state,
            approval_id="original-approval" if state == "awaiting_approval" else None,
        )
    before = repo.get(run["run_id"])
    restarted = CanonicalRunAdmissions(repo)
    fresh = await authenticate(resolver, _token())
    with pytest.raises(CurrentAdmissionRequired, match="not_unsent"):
        await restarted.reauthenticate(run["run_id"], fresh)
    assert await restarted.claim("worker", "boot1") is None
    assert repo.get(run["run_id"]) == before


@pytest.mark.parametrize("state", ["unknown", "completed"])
async def test_live_carrier_cannot_reopen_unknown_or_completed_product_custody(admitted, state):
    repo, gate, _principal, _resolver, _user = admitted
    await submit(admitted)
    claimed = await gate.claim("worker", "boot1")
    repo.transition(claimed["run_id"], "worker", "boot1", claimed["token"], state)
    with pytest.raises(CurrentAdmissionRequired, match="not_dispatchable"):
        await gate.current(claimed)
    assert repo.get(claimed["run_id"])["state"] == state


async def test_foreign_org_membership_cannot_take_original_human_request(admitted):
    repo, _gate, _principal, resolver, _user = admitted
    run = await submit(admitted)
    foreign = await authenticate(resolver, _token("sms_company_actual_member"))
    restarted = CanonicalRunAdmissions(repo)
    with pytest.raises(CurrentAdmissionRequired, match="actor_mismatch"):
        await restarted.reauthenticate(run["run_id"], foreign)
    assert await restarted.claim("worker", "boot1") is None
    assert repo.get(run["run_id"])["state"] == "queued"


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE human_accounts SET status='disabled'",
        "UPDATE users SET status='disabled'",
        "UPDATE users SET membership_revision=membership_revision+1",
        "UPDATE users SET role='user'",
    ],
)
async def test_revoked_or_changed_actor_is_not_claimed_after_queue_wait(
    admitted, legacy_db, mutation
):
    repo, gate, _principal, _resolver, _user = admitted
    run = await submit(admitted)
    with sqlite3.connect(legacy_db) as db:
        db.execute(mutation)
    assert await gate.claim("worker", "boot1") is None
    assert repo.get(run["run_id"])["state"] == "queued"


@pytest.mark.parametrize(
    "fake", [{"account_id": "caller"}, SimpleNamespace(revalidate=lambda: None)]
)
async def test_metadata_or_duck_typed_principal_cannot_rehydrate(admitted, fake):
    repo, gate, _principal, _resolver, _user = admitted
    run = await submit(admitted)
    with pytest.raises(CurrentAdmissionRequired):
        await gate.reauthenticate(run["run_id"], fake)
    assert repo.get(run["run_id"])["state"] == "queued"


async def test_opaque_principal_never_enters_serialized_request(admitted):
    repo, gate, principal, _resolver, user = admitted
    with pytest.raises(TypeError):
        await gate.submit(
            principal,
            user=user,
            agent="expert",
            thread="one",
            operation="chat",
            key="opaque",
            request={"principal": principal},
        )
    assert repo.by_request(user=user, operation="chat", key="opaque") is None


async def test_server_actor_attribution_cannot_be_supplied_by_caller(admitted):
    repo, gate, principal, _resolver, user = admitted
    with pytest.raises(CurrentAdmissionRequired):
        await gate.submit(
            principal,
            user=user,
            agent="expert",
            thread="one",
            operation="chat",
            key="forged",
            request={"canonical_actor": {"member_id": "forged"}},
        )
    assert repo.by_request(user=user, operation="chat", key="forged") is None


async def test_input_cannot_change_while_current_identity_is_awaited(admitted, monkeypatch):
    repo, gate, principal, _resolver, user = admitted
    entered, release = asyncio.Event(), asyncio.Event()
    original = type(principal).revalidate

    async def pause_current(value):
        entered.set()
        await release.wait()
        return await original(value)

    monkeypatch.setattr(type(principal), "revalidate", pause_current)
    request = {"mode": "harness", "request": {"text": "original"}}
    task = asyncio.create_task(
        gate.submit(
            principal,
            user=user,
            agent="expert",
            thread="one",
            operation="chat",
            key="snapshot-before-auth-wait",
            request=request,
        )
    )
    await entered.wait()
    request["request"]["text"] = "changed during auth"
    release.set()
    run = await task
    assert json.loads(repo.get(run["run_id"])["request_json"])["request"]["text"] == "original"


async def test_current_carrier_refuses_changed_original_request(admitted, product):
    repo, gate, _principal, _resolver, _user = admitted
    run = await submit(admitted)
    with product.transaction() as db:
        db.execute(
            "UPDATE execution_runs SET request_key='changed' WHERE run_id=?", (run["run_id"],)
        )
    assert await gate.claim("worker", "boot1") is None
    assert repo.get(run["run_id"])["state"] == "queued"


async def test_actor_and_target_resource_owner_are_separate_facts(admitted, product):
    repo, gate, principal, _resolver, _user = admitted
    target = UserRepo(product).create(username="target-projection-fixture", role="user")
    AgentRepo(product).create(agent_id="target", user_id=target, name="Target fixture")
    ThreadRepo(product).insert(
        thread_id="target-thread",
        agent_id="target",
        user_id=target,
        channel_type="dashboard",
        session_key="target-thread",
    )
    run = await gate.submit(
        principal,
        user=target,
        agent="target",
        thread="target-thread",
        operation="chat",
        key="different-owner",
        request={"mode": "harness"},
    )
    assert run["user_id"] == target != principal.identity.projection.local_user_id
    assert (
        json.loads(run["request_json"])["canonical_actor"]["member_id"]
        == principal.identity.snapshot.member.owner_id
    )
    # No handler/model/resource grant is exercised by storing these facts.
    assert repo.get(run["run_id"])["state"] == "queued"


async def test_real_worker_passes_same_private_caller_after_capacity_wait(admitted):
    repo, gate, principal, _resolver, user = admitted
    run = await submit(admitted)
    observed = []
    service = ExecutionService(
        repo,
        node="worker",
        incarnation="boot1",
        role="worker",
        capacity=1,
        human_admissions=gate,
    )

    async def handler(current, decision):
        observed.append((current["run_id"], await current_execution.get().canonical_principal()))
        yield {"type": "done"}

    service.handler = handler
    await service.start()
    try:
        async with asyncio.timeout(8):
            events = [event async for event in service.replay(run["run_id"], user=user)]
        assert observed == [(run["run_id"], principal)]
        assert events[-1]["payload"]["state"] == "completed"
        assert repo.get(run["run_id"])["state"] == "completed"
    finally:
        await service.close()


async def test_restored_worker_does_not_dispatch_without_fresh_human(admitted):
    repo, _gate, _principal, _resolver, _user = admitted
    run = await submit(admitted)
    restored = CanonicalRunAdmissions(repo)
    service = ExecutionService(
        repo, node="worker", incarnation="boot1", role="worker", human_admissions=restored
    )
    assert await service.human_admissions.claim("worker", "boot1") is None
    assert repo.get(run["run_id"])["state"] == "queued"
    await service.io.drain()


async def test_unadapted_worker_cannot_steal_canonical_attribution_only_work(admitted):
    repo, gate, _principal, _resolver, _user = admitted
    run = await submit(admitted)
    assert repo.claim("worker", "boot1") is None
    assert repo.get(run["run_id"])["state"] == "queued"
    claimed = await gate.claim("worker", "boot1")
    assert claimed["run_id"] == run["run_id"]


async def test_mutation_during_thread_wait_cannot_retarget_managed_input(manager, legacy_db):
    principal = await authenticate(_resolver(legacy_db, manager._repos.db), _token())
    seen = []

    class Backend(FixtureBackend):
        async def stream(self, agent, request, *, admission=None):
            seen.append((admission, request))
            yield {"type": "done"}

    registry = AgentManager(repos=manager._repos, paths=manager.paths, managed_backend=Backend())
    await registry.boot()
    request = {"thread_id": "one", "messages": [{"text": "original"}]}

    async def collect():
        return [part async for part in registry.stream("expert", request, admission=principal)]

    try:
        async with registry._thread_execution_lock("expert", "one"):
            task = asyncio.create_task(collect())
            await asyncio.sleep(0)
            request["thread_id"] = "foreign"
            request["messages"][0]["text"] = "changed"
        await task
        assert seen == [(principal, {"thread_id": "one", "messages": [{"text": "original"}]})]
    finally:
        await registry.shutdown()


@pytest.mark.parametrize("call", [False, True])
async def test_canonical_human_crosses_actual_manager_queue_and_private_provider(
    manager, legacy_db, call
):
    pool = manager._repos.db
    principal = await authenticate(_resolver(legacy_db, pool), _token())
    user = principal.identity.projection.local_user_id
    AgentRepo(pool).create(agent_id="expert", user_id=user, name="Fixture resource")
    ThreadRepo(pool).insert(
        thread_id="one",
        agent_id="expert",
        user_id=user,
        channel_type="dashboard",
        session_key="one",
    )
    repo = ExecutionRepo(pool)
    gate = CanonicalRunAdmissions(repo)
    service = ExecutionService(
        repo, node="worker", incarnation="boot1", role="worker", human_admissions=gate
    )
    seen = []

    class Backend(FixtureBackend):
        async def stream(self, agent, request, *, admission=None):
            seen.append((agent, admission))
            yield {"type": "token", "text": "fixture output"}

    backend = Backend()

    async def invoke_call(agent, request, *, admission=None):
        seen.append((agent, admission))
        return {"fixture": True}

    backend.call.side_effect = invoke_call
    registry = AgentManager(repos=manager._repos, paths=manager.paths, managed_backend=backend)
    await registry.boot()
    services = build_shared_services(db=pool, paths=manager.paths, config=MossworkConfig())

    class LocalResources:
        async def authorize(self, *_):
            raise AssertionError("local integer resource grants cannot replace canonical provider")

    server = SimpleNamespace(
        services=services,
        employee_resources=LocalResources(),
        app_runtime=SimpleNamespace(
            agent_registry=registry, gateway=SimpleNamespace(processor=SimpleNamespace())
        ),
    )
    registry.execution = GatewayExecution(server, service)
    await service.start()
    try:
        request = {"thread_id": "one", "messages": [], "client_request_id": "same-original-request"}
        async with asyncio.timeout(8):
            if call:
                assert await registry.call("expert", request, admission=principal) == {
                    "fixture": True
                }
            else:
                frames = [
                    item async for item in registry.stream("expert", request, admission=principal)
                ]
                assert any(item.get("text") == "fixture output" for item in frames)
        assert seen == [("expert", principal)]
        run = repo.by_request(
            user=user, operation="call" if call else "cron", key="same-original-request"
        )
        assert run["state"] == "completed"
        assert _token() not in run["request_json"]
        assert (
            json.loads(run["request_json"])["canonical_actor"]["member_id"]
            == principal.identity.snapshot.member.owner_id
        )
    finally:
        await service.close()
        await registry.shutdown()


@pytest.mark.parametrize("operation", ["stream", "call", "hitl"])
async def test_manager_private_admission_runs_after_thread_lock(manager, legacy_db, operation):
    principal = await authenticate(_resolver(legacy_db, manager._repos.db), _token())
    seen = []

    class Backend(FixtureBackend):
        async def stream(self, agent, request, *, admission=None):
            seen.append(admission)
            yield {"type": "done"}

        async def resume_hitl(self, agent, thread, decisions, *, admission=None):
            seen.append(admission)
            yield {"type": "done"}

    backend = Backend()

    async def call(agent, request, *, admission=None):
        seen.append(admission)
        return {"fixture": True}

    backend.call.side_effect = call
    registry = AgentManager(repos=manager._repos, paths=manager.paths, managed_backend=backend)
    await registry.boot()

    async def invoke():
        if operation == "call":
            return await registry.call("expert", {"thread_id": "one"}, admission=principal)
        stream = (
            registry.stream("expert", {"thread_id": "one"}, admission=principal)
            if operation == "stream"
            else registry.resume_hitl("expert", "one", [], admission=principal)
        )
        return [chunk async for chunk in stream]

    try:
        # Acquire the actual product thread lock. Authentication changes while
        # the invocation is queued, so checking only at ingress would be stale.
        async with registry._thread_execution_lock("expert", "one"):
            task = asyncio.create_task(invoke())
            await asyncio.sleep(0)
            assert seen == []
            with sqlite3.connect(legacy_db) as db:
                db.execute("UPDATE users SET membership_revision=membership_revision+1")
        with pytest.raises(MossworkError):
            await task
        assert seen == []
    finally:
        await registry.shutdown()
