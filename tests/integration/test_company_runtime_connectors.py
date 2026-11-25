"""Integration coverage for desktop connector broker routes."""

from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from octop.api.app import _install_exception_handlers
from octop.api.middleware.jwt_auth import install
from octop.api.routers.company_runtime import router
from octop.config import MossworkConfig
from octop.infra.company_runtime.client import CompanyClient
from octop.infra.connectors.service import ConnectorService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


class _FakeRemoteTool:
    def __init__(self, name: str, result: str = "remote result", error: Exception | None = None):
        self.name = name
        self.description = "remote search tool"
        self.args_schema = {"type": "object", "properties": {}}
        self._result = result
        self._error = error

    async def ainvoke(self, arguments):
        if self._error is not None:
            raise self._error
        return self._result


@pytest.fixture
def company(tmp_path):
    db = SqlitePool(tmp_path / "company.db")
    run_migrations(db)
    services = build_shared_services(
        paths=PathLayout(tmp_path), config=MossworkConfig(company_runtime_enabled=True), db=db
    )
    owner = services.user_repo.create(username="employee", role="user")
    services.user_policy_repo.set(
        owner, "model_access", json.dumps({"model_refs": ["test/model"], "revision": "fixture"})
    )
    services.agent_repo.create(agent_id="expert", user_id=owner, name="Test Expert")
    services.thread_repo.insert(
        thread_id="thread1",
        agent_id="expert",
        user_id=owner,
        channel_type="dashboard",
        session_key="thread1",
        last_active=0,
    )
    services.thread_repo.insert(
        thread_id="thread2",
        agent_id="expert",
        user_id=owner,
        channel_type="dashboard",
        session_key="thread2",
        last_active=0,
    )
    credential = "mosswork-device.test1"
    now = int(time.time())
    services.device_repo.issue_code("code0", owner, "session0", now + 100, now + 3600)
    services.device_repo.pair(
        "code0",
        "device0",
        hashlib.sha256(credential.encode()).hexdigest(),
        "Test",
        "darwin",
        2,
        now,
    )
    server = SimpleNamespace(_started=True, services=services)
    app = FastAPI()
    app.state.mosswork_server = server
    app.include_router(router, prefix="/api")
    _install_exception_handlers(app)
    install(app, server)
    client = CompanyClient(
        "http://company.test", credential, transport=httpx.ASGITransport(app=app)
    )
    yield server, client, [credential], owner
    db.close()


def _acquire_task(
    server, *, owner: int, task_id: str, servers: list[str], thread_id: str = "thread1"
) -> None:
    server.services.company_runtime_repo.acquire(
        task_id=task_id,
        thread_id=thread_id,
        owner=owner,
        agent_id="expert",
        device_id="device0",
        request={"mcp_servers": servers},
        expert={"models": ["test/model"]},
        installation_hash=hashlib.sha256(b"i").hexdigest(),
        recovery_hash=hashlib.sha256(b"r").hexdigest(),
    )


@pytest.mark.asyncio
async def test_company_runtime_connector_routes_enforce_task_selection(company, monkeypatch):
    server, client, _credentials, owner = company
    server.services.connector_repo.create(
        instance_id="qq1",
        user_id=owner,
        kind="qq-mail",
        display_name="Fixture mail",
        mcp_server_name="qq-mail__qq1",
    )
    ConnectorService(
        repo=server.services.connector_repo,
        secret_repo=server.services.secret_repo,
        settings_repo=server.services.settings_repo,
        config=server.services.config,
    ).encrypt_and_store(instance_id="qq1", payload={"username": "u", "password": "p"})
    monkeypatch.setattr(
        "octop.infra.company_runtime.connector_broker.mcp_tools_for_kind",
        lambda kind: [
            {"name": "search_emails", "description": "search", "inputSchema": {"type": "object"}},
            {"name": "send_email", "description": "send", "inputSchema": {"type": "object"}},
        ],
    )
    monkeypatch.setattr(
        "octop.infra.company_runtime.connector_broker.call_gateway_tool",
        lambda kind, creds, name, args: "mail result",
    )
    _acquire_task(server, owner=owner, task_id="connector-task", servers=["qq-mail__qq1"])
    listed = await client.request("tasks/connector-task/connectors/tools?servers=qq-mail__qq1")
    assert listed[0]["tool_name"] == "search_emails"
    with pytest.raises(Exception, match="CONNECTOR_BROKER_NOT_SELECTED"):
        await client.request("tasks/connector-task/connectors/tools?servers=unknown")
    result = await client.request(
        "tasks/connector-task/connectors/call",
        {
            "server_name": "qq-mail__qq1",
            "tool_name": "search_emails",
            "arguments": {"query": "ALL"},
            "tool_call_id": "call-1",
            "idempotency_key": "connector-task:call-1",
            "confirmed": False,
        },
    )
    assert result["status"] == "completed"
    assert result["content"] == "mail result"
    with pytest.raises(Exception, match="CONNECTOR_BROKER_CONFIRMATION_REQUIRED"):
        await client.request(
            "tasks/connector-task/connectors/call",
            {
                "server_name": "qq-mail__qq1",
                "tool_name": "send_email",
                "arguments": {"to": "user@example.test"},
                "tool_call_id": "call-write",
                "idempotency_key": "connector-task:call-write",
                "confirmed": False,
            },
        )
    audit_rows = server.services.audit_repo.query(action="company_connector.call", limit=5)
    assert audit_rows and audit_rows[0].actor == "employee"


@pytest.mark.asyncio
async def test_remote_connector_route_lists_calls_and_reports_unknown(company, monkeypatch):
    server, client, _credentials, owner = company
    server.services.connector_repo.create(
        instance_id="remote1",
        user_id=owner,
        kind="tencent-docs",
        display_name="Remote docs",
        mcp_server_name="tencent-docs__remote1",
    )
    ConnectorService(
        repo=server.services.connector_repo,
        secret_repo=server.services.secret_repo,
        settings_repo=server.services.settings_repo,
        config=server.services.config,
    ).encrypt_and_store(instance_id="remote1", payload={"token": "remote-token"})
    state = {"error": None}

    async def load(configs, errors=None):
        return [_FakeRemoteTool("tencent-docs__remote1_search", error=state["error"])]

    monkeypatch.setattr("moss_harness.mcp.aload_mcp_tools", load)
    _acquire_task(server, owner=owner, task_id="remote-task", servers=["tencent-docs__remote1"])
    listed = await client.request(
        "tasks/remote-task/connectors/tools?servers=tencent-docs__remote1"
    )
    assert listed[0]["tool_name"] == "search"
    completed = await client.request(
        "tasks/remote-task/connectors/call",
        {
            "server_name": "tencent-docs__remote1",
            "tool_name": "search",
            "arguments": {"query": "budget"},
            "tool_call_id": "remote-call",
            "idempotency_key": "remote-task:remote-call",
            "confirmed": False,
        },
    )
    assert completed["status"] == "completed"

    state["error"] = TimeoutError("remote timeout")
    _acquire_task(
        server,
        owner=owner,
        task_id="remote-timeout-task",
        servers=["tencent-docs__remote1"],
        thread_id="thread2",
    )
    unknown = await client.request(
        "tasks/remote-timeout-task/connectors/call",
        {
            "server_name": "tencent-docs__remote1",
            "tool_name": "search",
            "arguments": {"query": "again"},
            "tool_call_id": "remote-timeout",
            "idempotency_key": "remote-timeout-task:remote-timeout",
            "confirmed": False,
        },
    )
    assert unknown["status"] == "unknown"


@pytest.mark.asyncio
async def test_custom_connector_route_uses_streamable_http_loader(company, monkeypatch):
    server, client, _credentials, owner = company
    svc = ConnectorService(
        repo=server.services.connector_repo,
        secret_repo=server.services.secret_repo,
        settings_repo=server.services.settings_repo,
        config=server.services.config,
    )
    svc.put_custom_servers(
        owner,
        {
            "custom-search": {
                "transport": "streamable_http",
                "url": "https://mcp.example.test/search",
            }
        },
    )

    async def load(configs, errors=None):
        return [_FakeRemoteTool("custom-search_lookup")]

    monkeypatch.setattr("moss_harness.mcp.aload_mcp_tools", load)
    _acquire_task(server, owner=owner, task_id="custom-task", servers=["custom-search"])
    listed = await client.request("tasks/custom-task/connectors/tools?servers=custom-search")
    assert listed[0]["tool_name"] == "lookup"
    result = await client.request(
        "tasks/custom-task/connectors/call",
        {
            "server_name": "custom-search",
            "tool_name": "lookup",
            "arguments": {"query": "budget"},
            "tool_call_id": "custom-call",
            "idempotency_key": "custom-task:custom-call",
            "confirmed": False,
        },
    )
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_qcc_connector_route_uses_internal_mcp_dispatch(company, monkeypatch):
    server, client, _credentials, owner = company
    server.services.connector_repo.create(
        instance_id="qcc1",
        user_id=owner,
        kind="qcc",
        display_name="QCC",
        mcp_server_name="qcc__qcc1",
    )
    ConnectorService(
        repo=server.services.connector_repo,
        secret_repo=server.services.secret_repo,
        settings_repo=server.services.settings_repo,
        config=server.services.config,
    ).encrypt_and_store(instance_id="qcc1", payload={"api_key": "qcc-key"})

    async def qcc_request(self, instance_id, body):
        if body["method"] == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"tools": [{"name": "search__company", "description": "search"}]},
            }
        return {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {"content": [{"type": "text", "text": "qcc result"}]},
        }

    monkeypatch.setattr(ConnectorService, "handle_qcc_request", qcc_request)
    _acquire_task(server, owner=owner, task_id="qcc-task", servers=["qcc__qcc1"])
    listed = await client.request("tasks/qcc-task/connectors/tools?servers=qcc__qcc1")
    assert listed[0]["tool_name"] == "search__company"
    result = await client.request(
        "tasks/qcc-task/connectors/call",
        {
            "server_name": "qcc__qcc1",
            "tool_name": "search__company",
            "arguments": {"query": "acme"},
            "tool_call_id": "qcc-call",
            "idempotency_key": "qcc-task:qcc-call",
            "confirmed": False,
        },
    )
    assert result["status"] == "completed"
