"""Actual TCP/official MCP glue; reviewed identity and grants remain fixtures."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from tests.unit.connectors.test_fixed_json import reviewed_server, server

from octop.infra.connectors.fixed_json import FixedJsonError
from octop.infra.connectors.registered_model import RegisteredModelTransport
from octop.infra.connectors.tool_protocol import (
    ToolConnection,
    ToolDescriptor,
    ToolProtocolError,
    content_digest,
)
from octop.infra.sudograph.protocol import SudographError

REGISTRATION = object()
OPERATIONS = {
    "sudograph_context": {},
    "sudograph_context_read": {
        "id": "sg-source/widget",
        "revision": "a" * 64,
        "context_digest": "b" * 64,
    },
    "sudograph_model_contract": {},
    "sudograph_model_apply": {"base_revision": "r", "operation_id": "original", "model": "yaml"},
}


def descriptor(name):
    tool = {
        "name": name,
        "inputSchema": {
            "type": "object",
            "properties": {key: {"type": "string"} for key in OPERATIONS[name]},
            "additionalProperties": False,
        },
    }
    return ToolDescriptor(
        "2025-06-18", tool, content_digest({"protocol": "2025-06-18", "tool": tool})
    )


async def current():
    return None


def adapter(connection, reviewed, assertion=current, *, tool_resolver=None, review_resolver=None):
    async def tool(registered, operation):
        assert registered is REGISTRATION
        return connection, reviewed, assertion

    async def review(registered, request):
        assert registered is REGISTRATION
        return connection, assertion

    return RegisteredModelTransport(
        resolve_tool=tool_resolver or tool, resolve_review=review_resolver or review
    )


@pytest.mark.parametrize("name", OPERATIONS)
@pytest.mark.parametrize("is_error", [False, True])
async def test_real_mcp_preserves_original_operation_and_compiler_error(name, is_error):
    reviewed = descriptor(name)
    payload = {"code": "MODEL_INVALID", "reason": "Invalid", "recovery": "Repair"}
    state = {
        "tool": reviewed.tool,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": is_error},
    }
    async with server(reviewed_server(state)) as (port, requests):
        connection = ToolConnection(
            f"http://127.0.0.1:{port}/mcp", (("Authorization", "Bearer fixture"),)
        )
        result = await adapter(connection, reviewed).call_tool(REGISTRATION, name, OPERATIONS[name])
        assert result.is_error is is_error
        assert result.content == state["result"]["content"]
        assert state["calls"] == [{"name": name, "arguments": OPERATIONS[name]}]
        assert all(r["headers"]["authorization"] == "bearer fixture" for r in requests)


async def test_wrong_operation_descriptor_refuses_before_any_connection():
    state = {}
    async with server(reviewed_server(state)) as (port, requests):
        client = adapter(
            ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), descriptor("sudograph_context")
        )
        with pytest.raises(ToolProtocolError, match="TOOL_DESCRIPTOR_CHANGED"):
            await client.call_tool(REGISTRATION, "sudograph_model_contract", {})
        assert requests == []


@pytest.mark.parametrize("verdict", [False, True, {}, "allowed"])
async def test_malformed_current_assertion_sends_no_authenticated_bytes(verdict):
    async def malformed():
        return verdict

    async with server(reviewed_server({})) as (port, requests):
        client = adapter(
            ToolConnection(f"http://127.0.0.1:{port}/mcp", ()),
            descriptor("sudograph_context"),
            malformed,
        )
        with pytest.raises(FixedJsonError, match="EGRESS_DENIED"):
            await client.call_tool(REGISTRATION, "sudograph_context", {})
        assert requests == []


async def test_actual_post_write_revocation_is_uncertain_without_replay():
    reviewed = descriptor("sudograph_model_apply")
    state = {"tool": reviewed.tool}

    async def granted():
        if state.get("calls"):
            raise PermissionError("private grant diagnostic")

    async with server(reviewed_server(state)) as (port, _):
        client = adapter(ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), reviewed, granted)
        with pytest.raises(SudographError) as failure:
            await client.call_tool(
                REGISTRATION, "sudograph_model_apply", OPERATIONS["sudograph_model_apply"]
            )
        assert failure.value.code == "MODEL_WRITE_UNCONFIRMED"
        assert "original operation_id" in failure.value.recovery
        assert state["calls"] == [
            {"name": "sudograph_model_apply", "arguments": OPERATIONS["sudograph_model_apply"]}
        ]


@pytest.mark.parametrize("failure", ["nontext", "too-many-parts", "http-failure"])
async def test_lower_refusal_before_tool_result_preserves_possible_original_write(failure):
    name = "sudograph_model_apply"
    reviewed = descriptor(name)
    state = {
        "tool": reviewed.tool,
        "result": {
            "content": (
                [{"type": "image", "data": "AA==", "mimeType": "image/png"}]
                if failure == "nontext"
                else [{"type": "text", "text": "part"}]
                * (129 if failure == "too-many-parts" else 1)
            ),
        },
    }
    original = reviewed_server(state)

    def respond(request):
        reply = original(request)
        if (
            request["line"].startswith("POST")
            and json.loads(request["body"]).get("method") == "tools/call"
            and failure == "http-failure"
        ):
            return 503, {"Content-Type": "application/json"}, b"{}"
        return reply

    async with server(respond) as (port, _):
        client = adapter(ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), reviewed)
        try:
            with pytest.raises(SudographError) as failed:
                await client.call_tool(REGISTRATION, name, OPERATIONS[name])
            assert failed.value.code == "MODEL_WRITE_UNCONFIRMED"
            assert "original operation_id" in failed.value.recovery
        finally:
            assert state["calls"] == [{"name": name, "arguments": OPERATIONS[name]}]


async def test_failed_discovery_does_not_claim_a_model_write_attempt():
    name = "sudograph_model_apply"
    reviewed = descriptor(name)
    state = {"tool": reviewed.tool}
    original = reviewed_server(state)

    def respond(request):
        reply = original(request)
        if (
            request["line"].startswith("POST")
            and json.loads(request["body"]).get("method") == "tools/list"
        ):
            return 503, {"Content-Type": "application/json"}, b"{}"
        return reply

    async with server(respond) as (port, _):
        client = adapter(ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), reviewed)
        with pytest.raises(SudographError) as failed:
            await client.call_tool(REGISTRATION, name, OPERATIONS[name])
        assert failed.value.code != "MODEL_WRITE_UNCONFIRMED"
        assert "original operation_id" not in failed.value.recovery
        assert state.get("calls", []) == []


async def test_revocation_during_transport_close_withholds_original_write_reply():
    reviewed = descriptor("sudograph_model_apply")
    state = {"tool": reviewed.tool}
    respond = reviewed_server(state)

    def close_revokes(request):
        result = respond(request)
        if request["line"].startswith("DELETE"):
            state["closed"] = True
        return result

    async def granted():
        if state.get("closed"):
            raise PermissionError("private closed grant")

    async with server(close_revokes) as (port, _):
        client = adapter(ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), reviewed, granted)
        with pytest.raises(SudographError) as failure:
            await client.call_tool(
                REGISTRATION, "sudograph_model_apply", OPERATIONS["sudograph_model_apply"]
            )
        assert failure.value.code == "MODEL_WRITE_UNCONFIRMED"
        assert len(state["calls"]) == 1
        assert state["closed"]


async def test_arguments_are_captured_before_waiting_for_actual_resolver():
    reviewed = descriptor("sudograph_model_apply")
    state = {"tool": reviewed.tool}
    entered, release = asyncio.Event(), asyncio.Event()
    async with server(reviewed_server(state)) as (port, _):
        connection = ToolConnection(f"http://127.0.0.1:{port}/mcp", ())

        async def resolve(registered, name):
            assert registered is REGISTRATION
            entered.set()
            await release.wait()
            return connection, reviewed, current

        client = adapter(connection, reviewed, tool_resolver=resolve)
        request = copy.deepcopy(OPERATIONS["sudograph_model_apply"])
        task = asyncio.create_task(client.call_tool(REGISTRATION, "sudograph_model_apply", request))
        await asyncio.wait_for(entered.wait(), 2)
        request["operation_id"] = "replacement"
        release.set()
        await task
        assert state["calls"] == [
            {"name": "sudograph_model_apply", "arguments": OPERATIONS["sudograph_model_apply"]}
        ]


async def test_changed_live_descriptor_prevents_effect():
    reviewed = descriptor("sudograph_model_apply")
    changed = copy.deepcopy(reviewed.tool)
    changed["description"] = "Changed after review"
    state = {"tool": changed}
    async with server(reviewed_server(state)) as (port, _):
        client = adapter(ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), reviewed)
        with pytest.raises(ToolProtocolError, match="TOOL_DESCRIPTOR_CHANGED"):
            await client.call_tool(
                REGISTRATION, "sudograph_model_apply", OPERATIONS["sudograph_model_apply"]
            )
        assert state.get("calls", []) == []


async def test_cancelled_actual_send_keeps_original_task_and_one_operation():
    reviewed = descriptor("sudograph_model_apply")
    state = {"tool": reviewed.tool}
    entered, release = asyncio.Event(), asyncio.Event()
    respond = reviewed_server(state)

    async def delayed(request):
        result = respond(request)
        if state.get("calls"):
            entered.set()
            await release.wait()
        return result

    async with server(delayed) as (port, _):
        client = adapter(ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), reviewed)
        task = asyncio.create_task(
            client.call_tool(
                REGISTRATION, "sudograph_model_apply", OPERATIONS["sudograph_model_apply"]
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            assert len(state["calls"]) == 1
        finally:
            release.set()


@pytest.mark.parametrize("path,expected", [("/mcp", "/review"), ("/nested/mcp", "/nested/review")])
async def test_review_uses_original_relative_endpoint_and_exact_body(path, expected):
    payload = {"format": "sudograph-worker-review/v2", "result": {"html": "<html>原始</html>"}}
    async with server(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, requests):
        connection = ToolConnection(
            f"http://127.0.0.1:{port}{path}", (("Authorization", "Bearer fixture"),)
        )
        client = adapter(connection, descriptor("sudograph_context"))
        body = {"operation": "head", "resource": "original-project"}
        assert await client.read_review(REGISTRATION, body) == payload
        assert len(requests) == 1
        assert requests[0]["line"] == f"POST {expected} HTTP/1.1"
        assert json.loads(requests[0]["body"]) == body
        assert requests[0]["headers"]["authorization"] == "bearer fixture"


@pytest.mark.parametrize("failure", ["profile", "revoked", "redirect"])
async def test_review_refuses_invalid_profile_late_revoke_and_redirect(failure):
    payload = {"format": "wrong" if failure == "profile" else "sudograph-worker-review/v2"}
    state = {"sent": False}

    def respond(_):
        state["sent"] = True
        return (
            (302 if failure == "redirect" else 200),
            {"Content-Type": "application/json", "Location": "http://127.0.0.1:1/elsewhere"},
            json.dumps(payload).encode(),
        )

    async def granted():
        if failure == "revoked" and state["sent"]:
            raise PermissionError("private grant diagnostic")

    async with server(respond) as (port, requests):
        client = adapter(
            ToolConnection(f"http://127.0.0.1:{port}/mcp", ()),
            descriptor("sudograph_context"),
            granted,
        )
        with pytest.raises((FixedJsonError, SudographError, PermissionError)):
            await client.read_review(REGISTRATION, {"operation": "head", "resource": "project"})
        assert len(requests) == 1


@pytest.mark.parametrize(
    "status,error,expected",
    [
        (401, "private SQL diagnostic", "ACCESS_DENIED"),
        (403, "REVISION_UNKNOWN", "ACCESS_DENIED"),
        (404, "REVISION_UNKNOWN", "REVISION_UNKNOWN"),
        (409, "CONTEXT_STALE", "CONTEXT_STALE"),
        (400, "READ_CONTRACT", "READ_CONTRACT"),
        (404, "TARGET_UNKNOWN", "TARGET_UNKNOWN"),
        (404, "CONTEXT_TARGET_UNKNOWN", "CONTEXT_TARGET_UNKNOWN"),
        (409, "SOURCE_SCHEMA_CHANGED", "SOURCE_SCHEMA_CHANGED"),
        (500, "private SQL diagnostic", "VIEW_UNAVAILABLE"),
        (500, {"private": "credential"}, "VIEW_UNAVAILABLE"),
    ],
)
async def test_review_preserves_only_original_protocol_error_codes(status, error, expected):
    async with server(
        lambda _: (
            status,
            {"Content-Type": "application/json"},
            json.dumps({"error": error}).encode(),
        )
    ) as (port, requests):
        client = adapter(
            ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), descriptor("sudograph_context")
        )
        with pytest.raises(SudographError) as failure:
            await client.read_review(REGISTRATION, {"operation": "head", "resource": "project"})
        assert failure.value.code == expected
        assert "private" not in str(failure.value)
        assert "private" not in failure.value.recovery
        assert len(requests) == 1
