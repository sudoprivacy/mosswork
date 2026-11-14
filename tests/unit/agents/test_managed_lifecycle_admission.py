"""Real sealed human admission at manager locks; backend/native effects are fixtures."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from tests.unit.agents.test_agent_manager import manager as manager
from tests.unit.agents.test_managed_execution_backend import FixtureBackend, selected
from tests.unit.identity.test_canonical_human_request_projection import _resolver, _token
from tests.unit.identity.test_canonical_human_request_projection import legacy_db as legacy_db

from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.execution import ExecutionRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.errors import MossworkError
from octop.infra.execution.admission import CanonicalRunAdmissions, CurrentAdmissionRequired
from octop.infra.execution.service import ActiveExecution, ExecutionService, current_execution
from octop.infra.identity.request_principal import authenticate
from octop.infra.native_host.context import HostUnavailable


@pytest.fixture
async def human(manager, legacy_db):
    resolver = _resolver(legacy_db, manager._repos.db)
    return await authenticate(resolver, _token())


async def effect(registry, operation, principal):
    if operation in {"set_model", "clear_model"}:
        await registry.update_thread_model(
            "expert",
            "one",
            "original-model" if operation == "set_model" else None,
            admission=principal,
        )
    else:
        await getattr(registry, operation)("expert", admission=principal)


def effect_mock(backend, operation):
    return getattr(
        backend,
        {"set_model": "set_thread_model", "clear_model": "clear_thread_model"}.get(
            operation, operation
        ),
    )


def effect_lock(registry, operation):
    if operation in {"set_model", "clear_model"}:
        return registry._thread_execution_lock("expert", "one")
    return registry._lifecycle_lock_for("expert")


@pytest.mark.parametrize("operation", ["start", "stop", "reload"])
async def test_missing_human_cannot_dispatch_managed_lifecycle(manager, operation):
    backend = FixtureBackend()
    registry = selected(manager, backend)
    await registry.boot()
    try:
        with pytest.raises(CurrentAdmissionRequired):
            await getattr(registry, operation)("expert")
        getattr(backend, operation).assert_not_awaited()
    finally:
        await registry.shutdown()


@pytest.mark.parametrize("clear", [False, True])
async def test_sync_model_mutation_cannot_dispatch_managed_effect(manager, clear):
    backend = FixtureBackend()
    registry = selected(manager, backend)
    await registry.boot()
    try:
        with pytest.raises(HostUnavailable, match="awaited current admission"):
            if clear:
                registry.clear_thread_model("expert", "one")
            else:
                registry.set_thread_model("expert", "one", "model")
        backend.set_thread_model.assert_not_called()
        backend.clear_thread_model.assert_not_called()
    finally:
        await registry.shutdown()


@pytest.mark.parametrize("model", [None, "original-model"])
async def test_awaited_model_write_still_requires_live_sealed_human(manager, model):
    backend = FixtureBackend()
    registry = selected(manager, backend)
    await registry.boot()
    try:
        with pytest.raises(CurrentAdmissionRequired):
            await registry.update_thread_model("expert", "one", model)
        backend.set_thread_model.assert_not_awaited()
        backend.clear_thread_model.assert_not_awaited()
    finally:
        await registry.shutdown()


@pytest.mark.parametrize("operation", ["start", "stop", "reload", "set_model", "clear_model"])
async def test_same_sealed_human_reaches_awaited_effect_port(manager, human, operation):
    backend = FixtureBackend()
    registry = selected(manager, backend)
    await registry.boot()
    try:
        await effect(registry, operation, human)
        called = effect_mock(backend, operation)
        called.assert_awaited_once()
        assert called.await_args.kwargs["admission"] is human
        assert called.await_args.args[0] == "expert"
        if operation in {"set_model", "clear_model"}:
            assert called.await_args.args[1] == "one"
        if operation == "reload":
            assert called.await_args.kwargs["except_run"] == ""
    finally:
        await registry.shutdown()


@pytest.mark.parametrize("operation", ["start", "stop", "reload", "set_model", "clear_model"])
async def test_revocation_during_manager_lock_wait_refuses_before_provider(
    manager, human, legacy_db, operation
):
    backend = FixtureBackend()
    registry = selected(manager, backend)
    await registry.boot()
    try:
        async with effect_lock(registry, operation):
            task = asyncio.create_task(effect(registry, operation, human))
            await asyncio.sleep(0)
            assert not task.done()
            with sqlite3.connect(legacy_db) as db:
                db.execute("UPDATE human_accounts SET status='disabled'")
            effect_mock(backend, operation).assert_not_awaited()
        with pytest.raises(MossworkError):
            await task
        effect_mock(backend, operation).assert_not_awaited()
    finally:
        await registry.shutdown()


@pytest.mark.parametrize("operation", ["start", "set_model"])
async def test_cancelled_lock_wait_has_no_effect_or_retry(manager, human, operation):
    backend = FixtureBackend()
    registry = selected(manager, backend)
    await registry.boot()
    try:
        async with effect_lock(registry, operation):
            task = asyncio.create_task(effect(registry, operation, human))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert task.cancelled()
        effect_mock(backend, operation).assert_not_awaited()
    finally:
        await registry.shutdown()


@pytest.mark.parametrize("fake", [{"role": "admin"}, SimpleNamespace(revalidate=lambda: None)])
async def test_caller_metadata_cannot_replace_sealed_effect_admission(manager, fake):
    backend = FixtureBackend()
    registry = selected(manager, backend)
    await registry.boot()
    try:
        with pytest.raises(CurrentAdmissionRequired):
            await registry.stop("expert", admission=fake)
        backend.stop.assert_not_awaited()
    finally:
        await registry.shutdown()


@pytest.mark.parametrize("operation", ["reload", "set_model"])
async def test_possible_effect_error_or_invalid_completion_does_not_retry(
    manager, human, operation
):
    backend = FixtureBackend()
    called = effect_mock(backend, operation)
    if operation == "reload":
        called.side_effect = HostUnavailable("Original native outcome unknown")
    else:
        called.return_value = False
    registry = selected(manager, backend)
    await registry.boot()
    try:
        with pytest.raises(HostUnavailable):
            await effect(registry, operation, human)
        called.assert_awaited_once()
    finally:
        await registry.shutdown()


@pytest.mark.parametrize("change", [None, "foreign_agent", "different_principal", "unknown"])
async def test_original_queue_actor_is_recovered_and_uncertain_custody_refuses(
    manager, human, legacy_db, change
):
    pool = manager._repos.db
    user = human.identity.projection.local_user_id
    AgentRepo(pool).create(agent_id="expert", user_id=user, name="Original")
    ThreadRepo(pool).insert(
        thread_id="one",
        agent_id="expert",
        user_id=user,
        channel_type="dashboard",
        session_key="one",
    )
    repo = ExecutionRepo(pool)
    repo.register("worker", "boot1", "worker")
    gate = CanonicalRunAdmissions(repo)
    await gate.submit(
        human,
        user=user,
        agent="expert",
        thread="one",
        operation="chat",
        key="original",
        request={"request": {"text": "original"}},
    )
    run = await gate.claim("worker", "boot1")
    service = ExecutionService(
        repo, node="worker", incarnation="boot1", role="worker", human_admissions=gate
    )
    backend = FixtureBackend()
    registry = selected(manager, backend)
    await registry.boot()
    context_token = current_execution.set(ActiveExecution(service, run))
    try:
        supplied = None
        if change == "different_principal":
            supplied = await authenticate(_resolver(legacy_db, pool), _token())
        if change == "unknown":
            repo.transition(run["run_id"], "worker", "boot1", run["token"], "unknown")
        if change is None:
            await registry.start("expert")
            backend.start.assert_awaited_once_with("expert", admission=human)
        else:
            before = repo.get(run["run_id"])
            with pytest.raises(CurrentAdmissionRequired):
                await registry.start(
                    "foreign" if change == "foreign_agent" else "expert", admission=supplied
                )
            backend.start.assert_not_awaited()
            assert repo.get(run["run_id"]) == before
    finally:
        current_execution.reset(context_token)
        await registry.shutdown()
        await service.io.drain()


async def test_bulk_managed_reload_keeps_first_unknown_without_advancing_targets(manager, human):
    for agent in ("first", "second"):
        manager._repos.agent_repo.create(
            agent_id=agent, user_id=human.identity.projection.local_user_id, name=agent
        )
    backend = FixtureBackend()
    backend.reload.side_effect = HostUnavailable("Original native outcome unknown")
    registry = selected(manager, backend)
    await registry.boot()
    try:
        with pytest.raises(HostUnavailable, match="Original native outcome unknown"):
            await registry.reload_all(admission=human)
        backend.reload.assert_awaited_once()
        assert backend.reload.await_args.kwargs["admission"] is human
    finally:
        await registry.shutdown()
