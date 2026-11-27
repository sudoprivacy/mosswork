from dataclasses import replace
from importlib.metadata import version
from unittest.mock import AsyncMock

import pytest

from octop.infra.company_runtime import SNAPSHOT_PROTOCOL_HEADER
from octop.infra.company_runtime.compatibility import check_capabilities, validated_snapshot
from octop.infra.company_runtime.runtime import EnterpriseRuntime

from .test_runtime import install_agent


async def test_legacy_snapshot_selection_is_scoped_and_replayable(company):
    server, client, _, _ = company
    install_agent(server)
    body = {
        "agent_id": "expert",
        "thread_id": "thread1",
        "task_id": "first",
        "text": "hello",
        "installation_token": "x" * 32,
        "recovery_token": "r" * 32,
    }
    native = version("moss-harness")
    try:
        refused = await client.http.post(
            "tasks", json=body, headers={SNAPSHOT_PROTOCOL_HEADER: "2"}
        )
        assert refused.status_code == 409
        assert server.services.company_runtime_repo.task("first") is None
        del client.http.headers[SNAPSHOT_PROTOCOL_HEADER]
        first = (await client.request("tasks", body))["expert"]
        assert (first["harness_version"], first["server_harness_version"]) == ("0.0.8", native)
        server.services = replace(
            server.services,
            config=replace(
                server.services.config, company_runtime_snapshot_profiles={"device0": "server"}
            ),
        )
        assert (await client.request("tasks", body))["expert"] == first
        await client.request("tasks/first/status", {"status": "completed"})
        second = (await client.request("tasks", {**body, "task_id": "second"}))["expert"]
        assert second["harness_version"] == native and second["version"] != first["version"]
        await client.request("tasks/second/status", {"status": "completed"})
        server.services = replace(
            server.services,
            config=replace(server.services.config, company_runtime_snapshot_profiles={}),
        )
        inherited = (
            await client.request("tasks", {**body, "thread_id": "thread2", "task_id": "third"})
        )["expert"]
        assert inherited == second
        other = {
            **body,
            "installation_token": "y" * 32,
            "thread_id": "thread3",
            "task_id": "fourth",
        }
        assert (await client.request("tasks", other))["expert"] == first
        await client.request("tasks/fourth/status", {"status": "completed"})
        connectors = await client.request("tasks", {**other, "task_id": "fifth", "mcp_servers": []})
        assert connectors["expert"] == second
    finally:
        await client.close()


@pytest.mark.parametrize(
    "change",
    [
        {"protocol_version": 2},
        {"protocol_version": True},
        {"checkpoint_format": "unsupported"},
        {"checkpoint_format": None},
        {"features": []},
        {"features": "checkpoint_batches"},
    ],
)
async def test_incompatible_handshake_prevents_acquisition(company, tmp_path, monkeypatch, change):
    _, client, _, _ = company
    try:
        capabilities = await client.request("capabilities")
        check_capabilities(capabilities)
    finally:
        await client.close()
    peer = AsyncMock()
    peer.request.return_value = {**capabilities, **change}
    monkeypatch.setattr("octop.infra.company_runtime.runtime.CompanyClient", lambda *_: peer)
    runtime = EnterpriseRuntime(lambda _: None)
    workspace = tmp_path / "project"
    workspace.mkdir()
    with pytest.raises(RuntimeError, match="^company_protocol_incompatible$"):
        await runtime.initialize(
            {
                "workspace": workspace,
                "state_dir": tmp_path / "state",
                "company_url": "http://company.test",
                "credential": "fixture",
                "execute_authorized": True,
            }
        )
    assert runtime.client is None
    peer.request.assert_awaited_once_with("capabilities")
    peer.close.assert_awaited_once()


def test_snapshot_validates_consumed_fields_and_allows_extensions():
    snapshot = {
        "protocol_version": 1,
        "version": "a" * 64,
        "name": "Test expert",
        "instructions": "",
        "models": ["test/model"],
        "default_model": "test/model",
        "skills": [],
        "extra_metadata": {"future": True},
    }
    assert validated_snapshot(snapshot) == snapshot
    for change in (
        {"protocol_version": 2},
        {"protocol_version": True},
        {"version": "invalid"},
        {"models": "test/model"},
        {"default_model": "missing"},
        {"skills": [{"path": "skills/demo/SKILL.md"}]},
        {"capabilities": {"web_search": "false"}},
    ):
        with pytest.raises(RuntimeError, match="^company_protocol_incompatible$"):
            validated_snapshot({**snapshot, **change})
    del snapshot["instructions"]
    with pytest.raises(RuntimeError, match="^company_protocol_incompatible$"):
        validated_snapshot(snapshot)
