"""PRODUCT retention with real SQLite references; runtime effects remain mocks."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from octop.api.app import _install_exception_handlers
from octop.api.deps import current_user, get_server
from octop.api.routers import agents
from octop.api.routers.chat import history
from octop.config import MossworkConfig
from octop.infra.agents.manager import AgentManager
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.retained_cohost import RetainedBindingInvalid
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.retention import RetainedResourceError
from octop.infra.db.services import RepoBundle, build_shared_services
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout
from tests.unit.native_host.test_authority import _sql
from tests.unit.test_retained_session_selection import (
    bound_store as bound_store,
)
from tests.unit.test_retained_session_selection import (
    canonical_db as canonical_db,
)
from tests.unit.test_retained_session_selection import (
    mapped as mapped,
)
from tests.unit.test_retained_session_selection import (
    product as product,
)
from tests.unit.test_retained_session_selection import (
    record,
)


@pytest.mark.parametrize("resource", ["agent", "thread"])
@pytest.mark.parametrize("mapped_reference", [False, True])
async def test_retained_or_not_yet_mapped_canonical_resource_cannot_be_deleted(
    mapped, product, resource, mapped_reference
):
    ports, _actor, owner, _products, _selections = mapped
    if mapped_reference:
        await record(mapped)
    # Current access refusal is independent of preservation.
    with product.transaction() as conn:
        conn.execute("UPDATE users SET disabled=1 WHERE id=?", (owner.local_user_id,))
        conn.execute(
            "INSERT INTO thread_messages(thread_id,seq,message_id,role,message_json,created_at) "
            "VALUES (?,1,'original-message','user','{\"content\":\"retained\"}',1)",
            (ports.selection.thread_id,),
        )
    with product.connect() as conn:
        before = list(conn.iterdump())
    original_bytes = ports.path.read_bytes()
    repo = AgentRepo(product) if resource == "agent" else ThreadRepo(product)
    identifier = ports.selection.agent_id if resource == "agent" else ports.selection.thread_id
    with pytest.raises(RetainedResourceError):
        repo.delete(identifier)
    assert repo.get(identifier) is not None
    with product.connect() as conn:
        assert list(conn.iterdump()) == before
    assert ports.path.read_bytes() == original_bytes


async def test_original_change_after_reference_commit_does_not_enable_cleanup(
    mapped, product, monkeypatch
):
    ports, actor, _owner, products, _selections = mapped
    persist = products.record_existing

    def changed_after_commit(projection):
        persist(projection)
        _sql(ports.path, "UPDATE cohost_sessions SET durable_session_id='changed-history'")

    monkeypatch.setattr(products, "record_existing", changed_after_commit)
    with pytest.raises(RetainedBindingInvalid):
        await record(mapped)
    saved = products.get(actor.authority_id, ports.selection.agent_id, ports.selection.thread_id)
    for repo, identifier in [
        (AgentRepo(product), saved.agent_id),
        (ThreadRepo(product), saved.thread_id),
    ]:
        with pytest.raises(RetainedResourceError):
            repo.delete(identifier)
    assert products.get(actor.authority_id, saved.agent_id, saved.thread_id) == saved


async def test_raw_reference_still_protects_detached_product_owners(mapped, product):
    ports, _actor, _owner, _products, _selections = mapped
    await record(mapped)
    local_user = UserRepo(product).create(username="detached-local", role="user")
    with product.transaction() as conn:
        conn.execute("UPDATE agents SET user_id=NULL")
        conn.execute("UPDATE threads SET user_id=?", (local_user,))
    with pytest.raises(RetainedResourceError):
        AgentRepo(product).delete(ports.selection.agent_id)
    with pytest.raises(RetainedResourceError):
        ThreadRepo(product).delete(ports.selection.thread_id)


@pytest.fixture
def runtime(mapped, product, tmp_path):
    ports, _actor, owner, _products, _selections = mapped
    repos = RepoBundle.from_pool(product)
    manager = AgentManager(repos=repos, paths=PathLayout(tmp_path / "product-home"))
    manager._harness_manager = MagicMock(aremove_agent=AsyncMock())
    manager._harness_manager.get_agent.return_value.agent.adelete_thread = AsyncMock(
        return_value=True
    )
    gateway = SimpleNamespace(
        thread_registry=ThreadRegistry(
            session_repo=repos.session_repo, thread_repo=repos.thread_repo
        )
    )
    server = SimpleNamespace(app_runtime=SimpleNamespace(agent_registry=manager, gateway=gateway))
    return ports, owner, repos, manager, server


@pytest.mark.parametrize("operation", ["delete", "abort", "checkpoint"])
async def test_retention_refuses_before_runtime_files_or_checkpoint_effects(runtime, operation):
    ports, _owner, _repos, manager, _server = runtime
    workspace = manager.paths.ensure_agent_workspace(ports.selection.agent_id)
    marker = workspace / "original-history.txt"
    marker.write_text("retained history", encoding="utf-8")
    if operation == "abort":
        await manager._abort_incomplete_create(ports.selection.agent_id)
    else:
        with pytest.raises(RetainedResourceError):
            if operation == "delete":
                await manager.delete(ports.selection.agent_id)
            else:
                await manager.delete_thread_checkpoint(
                    *(ports.selection.agent_id, ports.selection.thread_id)
                )
    assert marker.read_text(encoding="utf-8") == "retained history"
    manager._harness_manager.aremove_agent.assert_not_awaited()
    manager._harness_manager.get_agent.assert_not_called()
    assert manager.get_row(ports.selection.agent_id) is not None


async def test_offline_delete_preserves_workspace(runtime, monkeypatch):
    from octop.cli.support import offline_ops

    ports, _owner, repos, manager, _server = runtime
    workspace = manager.paths.ensure_agent_workspace(ports.selection.agent_id)
    marker = workspace / "original-history.txt"
    marker.write_text("retained history", encoding="utf-8")
    monkeypatch.setattr(
        offline_ops,
        "open_cli_services",
        lambda _home: nullcontext(
            SimpleNamespace(agent_repo=repos.agent_repo, paths=manager.paths)
        ),
    )
    with pytest.raises(RetainedResourceError):
        offline_ops.delete_agent_offline(ports.selection.agent_id)
    assert marker.read_text(encoding="utf-8") == "retained history"


async def test_user_cascade_cannot_remove_canonical_directory_or_resources(runtime, product):
    ports, owner, repos, manager, _server = runtime
    services = build_shared_services(db=product, paths=manager.paths, config=MossworkConfig())
    users = UserManager(services)
    await users.boot()
    row = repos.user_repo.get(owner.local_user_id)
    directory = manager.paths.user_dir(row.username)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "history.txt"
    marker.write_text("original retained content", encoding="utf-8")
    with pytest.raises(RetainedResourceError):
        await users.remove(row.username)
    with pytest.raises(RetainedResourceError):
        repos.user_repo.delete(owner.local_user_id)
    assert row.username in users._users
    assert marker.read_text(encoding="utf-8") == "original retained content"
    assert repos.agent_repo.get(ports.selection.agent_id) is not None
    assert repos.thread_repo.get(ports.selection.thread_id) is not None


async def test_local_user_cascade_cannot_remove_a_detached_original_reference(mapped, product):
    ports, _actor, _owner, _products, _selections = mapped
    await record(mapped)
    users = UserRepo(product)
    local_user = users.create(username="local-deletion", role="user")
    with product.transaction() as conn:
        conn.execute("UPDATE agents SET user_id=?", (local_user,))
        conn.execute("UPDATE threads SET user_id=?", (local_user,))
    with pytest.raises(RetainedResourceError):
        users.delete(local_user)
    assert AgentRepo(product).get(ports.selection.agent_id) is not None


@pytest.mark.parametrize("resource", ["agent", "thread"])
@pytest.mark.parametrize("locale", ["en", "zh"])
async def test_actual_delete_routes_return_localized_conflict_without_effects(
    runtime, resource, locale
):
    ports, owner, _repos, manager, server = runtime
    app = FastAPI()
    _install_exception_handlers(app)
    app.include_router(agents.router, prefix="/api/agents")
    app.include_router(history.router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(
        id=owner.local_user_id, is_admin=False
    )
    app.dependency_overrides[get_server] = lambda: server
    path = f"/api/agents/{ports.selection.agent_id}"
    if resource == "thread":
        path += f"/threads/{ports.selection.thread_id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.delete(path, headers={"Accept-Language": locale})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "RESOURCE_RETAINED"
    assert ("保留" if locale == "zh" else "retained") in response.json()["error"]["message"]
    assert response.json()["error"]["details"] == {}
    manager._harness_manager.get_agent.assert_not_called()
    manager._harness_manager.aremove_agent.assert_not_awaited()


def test_unmapped_standalone_deletion_remains_available(product):
    agents, threads = AgentRepo(product), ThreadRepo(product)
    local_user = UserRepo(product).create(username="standalone", role="user")
    agents.create(agent_id="local-only", user_id=None, name="local")
    threads.insert(
        thread_id="local-thread",
        agent_id="local-only",
        user_id=local_user,
        channel_type="cli",
        session_key="local",
    )
    threads.delete("local-thread")
    agents.delete("local-only")
    assert threads.get("local-thread") is None
    assert agents.get("local-only") is None
