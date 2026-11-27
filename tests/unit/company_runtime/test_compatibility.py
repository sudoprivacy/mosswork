import hashlib
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from octop.infra.company_runtime import SNAPSHOT_PROTOCOL_HEADER
from octop.infra.company_runtime.compatibility import check_capabilities, validated_snapshot
from octop.infra.company_runtime.runtime import EnterpriseRuntime

from .test_runtime import acquire, install_agent


@pytest.mark.parametrize("installed", ["1.4.2", "2.0.0rc1"])
async def test_server_upgrade_preserves_legacy_installation(company, monkeypatch, installed):
    server, client, _, owner = company
    install_agent(server)
    del client.http.headers[SNAPSHOT_PROTOCOL_HEADER]
    monkeypatch.setattr("octop.infra.company_runtime.service.version", lambda _: installed)
    try:
        assert (await acquire(client))["expert"]["harness_version"] == installed
        await client.request("tasks/task1/status", {"status": "completed"})
        monkeypatch.setattr("octop.infra.company_runtime.service.version", lambda _: "3.1.0")
        client.http.headers[SNAPSHOT_PROTOCOL_HEADER] = "1"
        negotiated = (await acquire(client, "negotiated"))["expert"]
        assert negotiated["harness_version"] == "3.1.0"
        await client.request("tasks/negotiated/status", {"status": "completed"})
        del client.http.headers[SNAPSHOT_PROTOCOL_HEADER]
        resumed = (await acquire(client, "resumed", "thread2"))["expert"]
        assert resumed["harness_version"] == installed
        assert resumed["server_harness_version"] == "3.1.0"
        assert resumed["version"] != negotiated["version"]
        installation = hashlib.sha256(("i" * 32).encode()).hexdigest()
        repo = server.services.company_runtime_repo
        assert repo.last_completed_harness(owner + 1, installation) is None
    finally:
        await client.close()


async def test_legacy_profile_precedence_and_replay(company, monkeypatch):
    server, client, _, _ = company
    install_agent(server)
    monkeypatch.setattr("octop.infra.company_runtime.service.version", lambda _: "4.0.0")

    def profiles(mapping):
        server.services = replace(
            server.services,
            config=replace(server.services.config, company_runtime_snapshot_profiles=mapping),
        )

    body = {
        "agent_id": "expert",
        "thread_id": "thread1",
        "task_id": "first",
        "text": "hello",
        "installation_token": "x" * 32,
        "recovery_token": "r" * 32,
    }
    profiles({"*": "1.3.0", "device0": "1.2.1"})
    try:
        refused = await client.http.post(
            "tasks", json=body, headers={SNAPSHOT_PROTOCOL_HEADER: "2"}
        )
        assert refused.status_code == 409
        assert server.services.company_runtime_repo.task("first") is None
        del client.http.headers[SNAPSHOT_PROTOCOL_HEADER]
        first = (await client.request("tasks", body))["expert"]
        assert (first["harness_version"], first["server_harness_version"]) == ("1.2.1", "4.0.0")
        profiles({"device0": "server"})
        assert (await client.request("tasks", body))["expert"] == first
        await client.request("tasks/first/status", {"status": "completed"})
        second = (await client.request("tasks", {**body, "task_id": "second"}))["expert"]
        assert second["harness_version"] == "4.0.0" and second["version"] != first["version"]
        await client.request("tasks/second/status", {"status": "completed"})
        profiles({"*": "1.3.0"})
        inherited = (
            await client.request("tasks", {**body, "thread_id": "thread2", "task_id": "third"})
        )["expert"]
        assert inherited == second
        other = {
            **body,
            "installation_token": "y" * 32,
            "thread_id": "thread3",
            "task_id": "fourth",
            "mcp_servers": [],
        }
        assert (await client.request("tasks", other))["expert"]["harness_version"] == "1.3.0"
        await client.request("tasks/fourth/status", {"status": "completed"})
        profiles({"device0": "1.2.1"})
        client.http.headers[SNAPSHOT_PROTOCOL_HEADER] = "1"
        negotiated = (await client.request("tasks", {**other, "task_id": "fifth"}))["expert"]
        assert negotiated["harness_version"] == "4.0.0"
    finally:
        await client.close()


async def test_legacy_client_rejects_incompatible_wire_format(company, monkeypatch):
    server, client, _, _ = company
    install_agent(server)
    del client.http.headers[SNAPSHOT_PROTOCOL_HEADER]
    monkeypatch.setattr("octop.infra.company_runtime.service.CHECKPOINT_FORMAT", "future-format")
    try:
        response = await client.http.post(
            "tasks",
            json={
                "agent_id": "expert",
                "thread_id": "thread1",
                "task_id": "incompatible",
                "text": "hello",
                "installation_token": "x" * 32,
                "recovery_token": "r" * 32,
            },
        )
        assert response.status_code == 409
        assert server.services.company_runtime_repo.task("incompatible") is None
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


async def test_unavailable_task_feature_fails_before_acquisition(company, tmp_path):
    from .test_memory_approvals import native_runtime

    server, client, _, _ = company
    runtime = native_runtime(client, tmp_path, [])
    runtime.task_features = set()
    try:
        with pytest.raises(RuntimeError, match="^feature_not_supported$"):
            await runtime.start(
                {
                    "agent_id": "expert",
                    "thread_id": "thread1",
                    "task_id": "feature",
                    "text": "hello",
                    "mcp_servers": ["mail"],
                }
            )
        assert not runtime.tasks
        assert server.services.company_runtime_repo.task("feature") is None
    finally:
        await client.close()


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
