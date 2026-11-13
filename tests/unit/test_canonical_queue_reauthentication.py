"""Existing human issuer and queue; no native or action grant is exercised."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.canonical_identity import CanonicalIdentityProjectionRepo
from octop.infra.db.repos.execution import ExecutionRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.errors import MossworkError
from octop.infra.execution.admission import CanonicalRunAdmissions, CurrentAdmissionRequired
from octop.infra.identity.projection import CanonicalRequestIdentityResolver
from octop.infra.identity.request_principal import authenticate
from tests.unit.identity.test_canonical_human_request_projection import product as product
from tests.unit.identity.test_human_sessions import _login
from tests.unit.identity.test_human_sessions import legacy_db as legacy_db
from tests.unit.identity.test_human_sessions import sessions as sessions


@pytest.fixture
async def original_queue(sessions, product):
    pair = await _login(sessions)
    resolver = CanonicalRequestIdentityResolver(
        sessions.service.snapshots, CanonicalIdentityProjectionRepo(product)
    )
    principal = await authenticate(resolver, pair.access_token)
    target = UserRepo(product).create(username="existing-target-projection", role="user")
    assert target != principal.identity.projection.local_user_id
    AgentRepo(product).create(agent_id="target-agent", user_id=target, name="Existing resource")
    ThreadRepo(product).insert(
        thread_id="target-thread",
        agent_id="target-agent",
        user_id=target,
        channel_type="dashboard",
        session_key="original-reference",
    )
    repo = ExecutionRepo(product)
    repo.register("same-worker", "same-boot", "worker")
    previous = CanonicalRunAdmissions(repo)
    run = await previous.submit(
        principal,
        user=target,
        agent="target-agent",
        thread="target-thread",
        operation="chat",
        key="original-unsent-operation",
        request={"mode": "harness", "request": {"text": "original input"}},
    )
    fresh = await sessions.service.refresh(pair.refresh_token)
    return SimpleNamespace(
        repo=repo,
        previous=previous,
        gate=CanonicalRunAdmissions(repo),
        resolver=resolver,
        principal=principal,
        run=run,
        fresh=fresh,
        product=product,
    )


async def test_fresh_credential_recovers_original_actor_separately_from_target_owner(
    original_queue,
):
    case = original_queue
    assert await case.gate.claim("same-worker", "same-boot") is None
    before = case.repo.get(case.run["run_id"])
    await case.gate.reauthenticate_human(
        before["run_id"], case.fresh.access_token, resolver=case.resolver
    )
    assert case.repo.get(before["run_id"]) == before
    claimed = await case.gate.claim("same-worker", "same-boot")
    assert claimed["run_id"] == before["run_id"]
    held = await case.gate.current(claimed)
    assert held is not case.principal
    assert held.identity.projection == case.principal.identity.projection
    assert held.identity.snapshot.credential_id != case.principal.identity.snapshot.credential_id
    assert held.identity.projection.local_user_id != claimed["user_id"]
    assert claimed["request_json"] == before["request_json"]
    assert case.fresh.access_token not in claimed["request_json"]
    assert case.fresh.refresh_token not in claimed["request_json"]
    assert UserRepo(case.product).count() == 2


async def test_missing_actor_projection_refuses_without_allocation(original_queue):
    case = original_queue
    with case.product.transaction() as connection:
        connection.execute("DELETE FROM canonical_user_projections")
    before = case.repo.get(case.run["run_id"])
    with pytest.raises(MossworkError):
        await case.gate.reauthenticate_human(
            before["run_id"], case.fresh.access_token, resolver=case.resolver
        )
    assert case.repo.get(before["run_id"]) == before
    assert await case.gate.claim("same-worker", "same-boot") is None
    assert UserRepo(case.product).count() == 2
    with case.product.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM canonical_user_projections").fetchone()[0] == 0
        )


@pytest.mark.parametrize("state", ["running", "unknown", "awaiting_approval", "completed"])
async def test_submitted_custody_refuses_before_new_credential_verification(
    original_queue, monkeypatch, state
):
    case = original_queue
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

    async def unexpected(*args):
        pytest.fail("Already submitted work must not reach the fresh actor helper")

    monkeypatch.setattr(case.resolver, "reauthenticate", unexpected)
    before = case.repo.get(claimed["run_id"])
    with pytest.raises(CurrentAdmissionRequired, match="not_unsent"):
        await case.gate.reauthenticate_human(
            claimed["run_id"], case.fresh.access_token, resolver=case.resolver
        )
    assert case.repo.get(claimed["run_id"]) == before
    assert await case.gate.claim("same-worker", "same-boot") is None


async def test_original_request_cannot_change_during_fresh_credential_wait(
    original_queue, monkeypatch
):
    case = original_queue
    entered, release = asyncio.Event(), asyncio.Event()
    previous = case.resolver.reauthenticate

    async def waited(token, actor):
        entered.set()
        await release.wait()
        return await previous(token, actor)

    monkeypatch.setattr(case.resolver, "reauthenticate", waited)
    task = asyncio.create_task(
        case.gate.reauthenticate_human(
            case.run["run_id"], case.fresh.access_token, resolver=case.resolver
        )
    )
    await entered.wait()
    replacement = json.loads(case.run["request_json"])
    replacement["request"]["text"] = "changed during reauthentication"
    with case.product.transaction() as connection:
        connection.execute(
            "UPDATE execution_runs SET request_json=? WHERE run_id=?",
            (json.dumps(replacement), case.run["run_id"]),
        )
    release.set()
    with pytest.raises(CurrentAdmissionRequired, match="original_queued_request_changed"):
        await task
    assert await case.gate.claim("same-worker", "same-boot") is None
    assert case.repo.get(case.run["run_id"])["state"] == "queued"


async def test_missing_original_actor_cannot_fall_back_to_resource_user_id(original_queue):
    case = original_queue
    payload = json.loads(case.run["request_json"])
    del payload["canonical_actor"]
    with case.product.transaction() as connection:
        connection.execute(
            "UPDATE execution_runs SET request_json=? WHERE run_id=?",
            (json.dumps(payload), case.run["run_id"]),
        )
    before = case.repo.get(case.run["run_id"])
    with pytest.raises(CurrentAdmissionRequired, match="original_request_actor_unavailable"):
        await case.gate.reauthenticate_human(
            before["run_id"], case.fresh.access_token, resolver=case.resolver
        )
    assert case.repo.get(before["run_id"]) == before
    assert await case.gate.claim("same-worker", "same-boot") is None
    assert UserRepo(case.product).count() == 2
