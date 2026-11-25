import json
from types import SimpleNamespace

import pytest

from octop.infra.company_runtime.connector_broker import ConnectorBroker
from octop.infra.errors import ErrorCode, MossworkError


class Audit:
    def __init__(self):
        self.rows = []

    def write(self, **kwargs):
        self.rows.append(kwargs)


class Service:
    owner = 7
    _config = SimpleNamespace()
    config = _config

    def __init__(self):
        self.calls = []
        self.kind = "qq-mail"
        self.resolve_calls = 0

    def resolve_server_names(self, user_id, names):
        assert user_id == 7
        self.resolve_calls += 1
        return {
            f"{self.kind}__one": {
                "server_name": f"{self.kind}__one",
                "instance_id": "one",
                "kind": self.kind,
                "mcp_mode": "gateway",
                "shared": False,
            }
        }

    async def ensure_fresh_credentials(self, instance_id, kind):
        return {"user": "u"}


@pytest.mark.asyncio
async def test_gateway_call_requires_confirmation_and_is_idempotent(monkeypatch):
    service = Service()
    audit = Audit()
    broker = ConnectorBroker(service=service, audit_repo=audit)
    monkeypatch.setattr(
        "octop.infra.company_runtime.connector_broker.mcp_tools_for_kind",
        lambda kind: [
            {"name": "send_email", "description": "send mail", "inputSchema": {"type": "object"}}
        ],
    )
    calls = []

    def call(kind, creds, name, args):
        calls.append((kind, name, args))
        return "sent"

    monkeypatch.setattr("octop.infra.company_runtime.connector_broker.call_gateway_tool", call)
    task = {
        "task_id": "task1",
        "request_json": json.dumps({"mcp_servers": ["qq-mail__one"]}),
    }
    with pytest.raises(MossworkError) as exc:
        await broker.call_tool(
            task,
            server_name="qq-mail__one",
            tool_name="send_email",
            arguments={"to": "a@example.test"},
            tool_call_id="call1",
            idempotency_key="task1:call1",
        )
    assert exc.value.code == ErrorCode.CONNECTOR_BROKER_CONFIRMATION_REQUIRED
    result = await broker.call_tool(
        task,
        server_name="qq-mail__one",
        tool_name="send_email",
        arguments={"to": "a@example.test"},
        tool_call_id="call1",
        idempotency_key="task1:call1",
        confirmed=True,
    )
    assert result == {"status": "completed", "content": "sent"}
    assert (
        await broker.call_tool(
            task,
            server_name="qq-mail__one",
            tool_name="send_email",
            arguments={"to": "a@example.test"},
            tool_call_id="call1",
            idempotency_key="task1:call1",
            confirmed=True,
        )
        == result
    )
    assert len(calls) == 1
    assert "a@example.test" in audit.rows[0]["payload"]


@pytest.mark.asyncio
async def test_unselected_connector_is_rejected():
    broker = ConnectorBroker(service=Service(), audit_repo=Audit())
    task = {"task_id": "task1", "request_json": json.dumps({"mcp_servers": []})}
    with pytest.raises(MossworkError) as exc:
        await broker.call_tool(
            task,
            server_name="qq-mail__one",
            tool_name="read_email",
            arguments={},
            tool_call_id="call1",
            idempotency_key="task1:call1",
        )
    assert exc.value.code == ErrorCode.CONNECTOR_BROKER_NOT_SELECTED


@pytest.mark.asyncio
async def test_gateway_method_side_effect_is_recomputed_for_call(monkeypatch):
    service = Service()
    service.kind = "feishu-cli"
    broker = ConnectorBroker(service=service, audit_repo=Audit())
    monkeypatch.setattr(
        "octop.infra.company_runtime.connector_broker.mcp_tools_for_kind",
        lambda kind: [{"name": "doc", "description": "docs", "inputSchema": {}}],
    )
    monkeypatch.setattr(
        "octop.infra.company_runtime.connector_broker.call_gateway_tool",
        lambda *args: "found",
    )
    task = {
        "task_id": "task1",
        "request_json": json.dumps({"mcp_servers": ["feishu-cli__one"]}),
    }
    result = await broker.call_tool(
        task,
        server_name="feishu-cli__one",
        tool_name="doc",
        arguments={"method": "+search", "args": {"query": "budget"}},
        tool_call_id="call-search",
        idempotency_key="task1:call-search",
    )
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_adapter_managed_gateway_write_skips_broker_confirmation(monkeypatch):
    service = Service()
    service.kind = "agently-cli"
    broker = ConnectorBroker(service=service, audit_repo=Audit())
    monkeypatch.setattr(
        "octop.infra.company_runtime.connector_broker.mcp_tools_for_kind",
        lambda kind: [{"name": "agently_send", "description": "send", "inputSchema": {}}],
    )
    monkeypatch.setattr(
        "octop.infra.company_runtime.connector_broker.call_gateway_tool",
        lambda *args: "adapter-confirmation-or-result",
    )
    task = {
        "task_id": "task1",
        "request_json": json.dumps({"mcp_servers": ["agently-cli__one"]}),
    }
    result = await broker.call_tool(
        task,
        server_name="agently-cli__one",
        tool_name="agently_send",
        arguments={"to": "user@example.test", "subject": "hi", "body": "hello"},
        tool_call_id="call-send",
        idempotency_key="task1:call-send",
    )
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_descriptor_list_is_cached_for_task(monkeypatch):
    service = Service()
    broker = ConnectorBroker(service=service, audit_repo=Audit())
    monkeypatch.setattr(
        "octop.infra.company_runtime.connector_broker.mcp_tools_for_kind",
        lambda kind: [{"name": "read_email", "description": "read", "inputSchema": {}}],
    )
    task = {
        "task_id": "task-cache",
        "request_json": json.dumps({"mcp_servers": ["qq-mail__one"]}),
    }
    await broker.list_task_tools(task, ["qq-mail__one"])
    await broker.list_task_tools(task, ["qq-mail__one"])
    assert service.resolve_calls == 1
