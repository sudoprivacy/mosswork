"""Actual manager/queue dispatch with an explicitly non-production backend fixture."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tests.unit.agents.test_agent_manager import manager as manager  # noqa: F401
from tests.unit.identity.test_canonical_human_request_projection import _resolver, _token
from tests.unit.identity.test_canonical_human_request_projection import legacy_db as legacy_db
from tests.unit.test_execution_service import setup as setup  # noqa: F401

from octop.config import MossworkConfig
from octop.infra.agents.manager import AgentManager
from octop.infra.db.services import build_shared_services
from octop.infra.execution.gateway import GatewayExecution
from octop.infra.execution.service import ExecutionService, current_execution
from octop.infra.identity.request_principal import authenticate
from octop.infra.native_host.context import HostUnavailable
from octop.infra.server import MossworkServer
from octop.infra.utils.paths import PathLayout


class FixtureBackend:
    """Records routing only; it supplies no native proof, model or grant."""

    def __init__(self):
        self.boot = AsyncMock(return_value=None)
        self.aclose = AsyncMock(return_value=None)
        self.start = AsyncMock(return_value=None)
        self.stop = AsyncMock(return_value=None)
        self.reload = AsyncMock(return_value=None)
        self.call = AsyncMock(return_value={"fixture": True})
        self.request_cancel = Mock()
        self.get_thread_model = Mock(return_value="fixture-model")
        self.set_thread_model = AsyncMock(return_value=None)
        self.clear_thread_model = AsyncMock(return_value=None)
        self.streams = []
        self.resumes = []

    async def stream(self, agent, request):
        self.streams.append((agent, request))
        yield {"type": "token", "content": "fixture output"}

    async def resume_hitl(self, agent, thread, decisions):
        self.resumes.append((agent, thread, decisions))
        yield {"type": "done"}


def selected(manager, backend):
    return AgentManager(repos=manager._repos, paths=manager.paths, managed_backend=backend)


async def test_selected_backend_boot_never_initializes_legacy_harness(manager, monkeypatch):
    backend = FixtureBackend()
    registry = selected(manager, backend)
    legacy = Mock(side_effect=AssertionError("legacy harness must not initialize"))
    monkeypatch.setattr("octop.infra.agents.manager.HarnessAgentManager", legacy)
    await registry.boot()
    backend.boot.assert_awaited_once()
    legacy.assert_not_called()
    assert registry._harness_manager is None
    await registry.shutdown()
    backend.aclose.assert_awaited_once()


async def test_selected_backend_error_does_not_fall_through_to_harness(manager):
    backend = FixtureBackend()
    backend.call.side_effect = RuntimeError("fixture managed result unknown")
    registry = selected(manager, backend)
    await registry.boot()
    legacy = SimpleNamespace(call=AsyncMock(), cancel=Mock())
    registry._harness_manager = legacy
    with pytest.raises(RuntimeError, match="managed result unknown"):
        await registry.call("expert", {"thread_id": "one"})
    backend.call.assert_awaited_once()
    legacy.call.assert_not_called()
    assert not registry.is_agent_active("expert")


async def test_stream_resume_and_cancel_route_to_the_same_selected_backend(manager):
    backend = FixtureBackend()
    registry = selected(manager, backend)
    await registry.boot()
    registry._harness_manager = SimpleNamespace(stream=Mock(), resume_hitl=Mock(), cancel=Mock())
    request = {"thread_id": "one", "messages": []}
    assert [chunk async for chunk in registry.stream("expert", request)] == [
        {"type": "token", "content": "fixture output"}
    ]
    decisions = [{"type": "reject"}]
    assert [chunk async for chunk in registry.resume_hitl("expert", "one", decisions)] == [
        {"type": "done"}
    ]
    registry.cancel_stream("expert", "one")
    assert backend.streams == [("expert", request)]
    assert backend.resumes == [("expert", "one", decisions)]
    backend.request_cancel.assert_called_once_with("expert", "one")
    registry._harness_manager.stream.assert_not_called()
    registry._harness_manager.resume_hitl.assert_not_called()
    registry._harness_manager.cancel.assert_not_called()


async def test_selected_backend_lifecycle_cannot_enter_legacy_start_stop_reload(manager, legacy_db):
    backend = FixtureBackend()
    registry = selected(manager, backend)
    await registry.boot()
    principal = await authenticate(_resolver(legacy_db, manager._repos.db), _token())
    await registry.start("expert", admission=principal)
    await registry.stop("expert", admission=principal)
    await registry._reload_agent("expert", except_run="current-product-run", admission=principal)
    backend.start.assert_awaited_once_with("expert", admission=principal)
    backend.stop.assert_awaited_once_with("expert", admission=principal)
    backend.reload.assert_awaited_once_with(
        "expert", except_run="current-product-run", admission=principal
    )


async def test_boot_failure_remains_failure_in_selected_mode(manager, monkeypatch):
    backend = FixtureBackend()
    backend.boot.side_effect = RuntimeError("fixture provider unavailable")
    registry = selected(manager, backend)
    legacy = Mock()
    monkeypatch.setattr("octop.infra.agents.manager.HarnessAgentManager", legacy)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await registry.boot()
    legacy.assert_not_called()
    assert registry.uses_managed_backend is True
    with pytest.raises(HostUnavailable):
        await registry.call("expert", {"thread_id": "one"})
    backend.call.assert_not_called()


async def test_model_preferences_use_selected_product_provider(manager, legacy_db):
    backend = FixtureBackend()
    registry = selected(manager, backend)
    await registry.boot()
    assert registry.get_thread_model("expert", "one") == "fixture-model"
    principal = await authenticate(_resolver(legacy_db, manager._repos.db), _token())
    await registry.update_thread_model("expert", "one", "other-fixture-model", admission=principal)
    await registry.update_thread_model("expert", "one", None, admission=principal)
    backend.get_thread_model.assert_called_once_with("expert", "one")
    backend.set_thread_model.assert_awaited_once_with(
        "expert", "one", "other-fixture-model", admission=principal
    )
    backend.clear_thread_model.assert_awaited_once_with("expert", "one", admission=principal)


async def test_invalid_boot_completion_cannot_enable_managed_dispatch(manager):
    backend = FixtureBackend()
    backend.boot.return_value = False
    registry = selected(manager, backend)
    with pytest.raises(HostUnavailable, match="Invalid host assertion completion"):
        await registry.boot()
    with pytest.raises(HostUnavailable):
        await registry.call("expert", {"thread_id": "one"})
    backend.call.assert_not_called()


async def test_closed_product_backend_never_reactivates_legacy_harness(manager):
    backend = FixtureBackend()
    registry = selected(manager, backend)
    await registry.boot()
    await registry.shutdown()
    with pytest.raises(HostUnavailable):
        await registry.call("expert", {"thread_id": "one"})
    assert registry.uses_managed_backend is True
    backend.call.assert_not_called()


async def test_real_server_boot_injects_one_backend_without_constructing_harness(
    tmp_path, monkeypatch
):
    from tests.support.app import ensure_control_plane_bound

    backend = FixtureBackend()
    legacy = Mock(side_effect=AssertionError("legacy harness constructed during real server boot"))
    monkeypatch.setattr("octop.infra.agents.manager.HarnessAgentManager", legacy)
    server = MossworkServer(home=tmp_path / "server", managed_backend=backend)
    try:
        await server.start()
        await ensure_control_plane_bound(server)
        assert server.app_runtime.agent_registry.uses_managed_backend is True
        backend.boot.assert_awaited_once()
        legacy.assert_not_called()
    finally:
        await server.stop()
    backend.aclose.assert_awaited_once()


@pytest.mark.parametrize("unknown", [False, True])
async def test_real_durable_queue_retains_selected_route_and_unknown_occupancy(
    setup, tmp_path, unknown
):
    pool, repo, user = setup
    paths = PathLayout(tmp_path / "product")
    services = build_shared_services(db=pool, paths=paths, config=MossworkConfig())
    backend = FixtureBackend()

    async def call(agent, request):
        active = current_execution.get()
        assert active is not None and active.run["thread_id"] == "one"
        assert agent == "expert" and request["thread_id"] == "one"
        if unknown:
            raise RuntimeError("fixture lost native response")
        return {"fixture": True}

    backend.call.side_effect = call
    registry = AgentManager(repos=services.repos, paths=paths, managed_backend=backend)
    await registry.boot()
    legacy_start = AsyncMock(side_effect=AssertionError("legacy registry.start preflight"))
    registry.start = legacy_start
    server = SimpleNamespace(
        services=services,
        employee_resources=SimpleNamespace(authorize=AsyncMock()),
        app_runtime=SimpleNamespace(
            agent_registry=registry, gateway=SimpleNamespace(processor=SimpleNamespace())
        ),
    )
    service = ExecutionService(repo, node="worker", incarnation="boot1", role="worker")
    adapter = GatewayExecution(server, service)
    registry.execution = adapter
    request = {"thread_id": "one", "client_request_id": "same-operation", "messages": []}
    await service.start()
    try:
        first = [frame async for frame in adapter.manager_stream("expert", request, call=True)]
        second = [frame async for frame in adapter.manager_stream("expert", request, call=True)]
        assert first == second
        backend.call.assert_awaited_once()
        legacy_start.assert_not_called()
        if unknown:
            active = repo.active("one")
            assert active is not None and active["state"] == "unknown"
        else:
            assert repo.active("one") is None
            assert first == [{"type": "execution.result", "result": {"fixture": True}}]
    finally:
        await service.close()
