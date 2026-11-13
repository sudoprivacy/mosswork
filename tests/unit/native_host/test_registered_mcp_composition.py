"""Real retained rows and TCP/MCP composition with explicit grant fixtures."""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from tests.unit.connectors.test_fixed_json import reviewed_server, server
from tests.unit.connectors.test_registered_model import OPERATIONS, descriptor
from tests.unit.db.test_retained_cohost import retained as retained
from tests.unit.db.test_retained_ontology import registered as registered
from tests.unit.native_host.test_reviewed_mcp import change, original
from tests.unit.native_host.test_reviewed_mcp import reviewed as reviewed

from octop.infra.connectors.fixed_json import FixedJsonError
from octop.infra.connectors.registered_model import RegisteredModelTransport
from octop.infra.connectors.tool_protocol import ToolProtocolError, content_digest
from octop.infra.db.repos.retained_mcp import RetainedReviewedMcpRepo
from octop.infra.db.repos.retained_ontology import RetainedOntologyBindingRepo
from octop.infra.native_host.reviewed_mcp import ReviewedModelResolvers
from octop.infra.sudograph.protocol import REVIEW_PROFILE, SudographError


def configure(path: Path, endpoint: str, name: str) -> dict:
    """Retain the original registration and review rows; change fixture endpoint."""
    tool = copy.deepcopy(descriptor(name).tool)
    tool["_meta"] = {"moss/host-review-profile": REVIEW_PROFILE}
    change(path, "UPDATE mcp_servers SET url = ?", (endpoint,))
    change(path, "UPDATE mcp_policies SET domain_whitelist_json = '[\"127.0.0.1\"]'")
    with closing(sqlite3.connect(path)) as db:
        raw = db.execute(
            "SELECT document FROM agent_tool_reviews WHERE resource = ?",
            (f"mcp:worker/{name}",),
        ).fetchone()
    review = json.loads(raw[0])
    review.update(
        protocol="2025-06-18",
        tool=tool,
        digest=content_digest({"protocol": "2025-06-18", "tool": tool}),
        serverDigest=content_digest(
            {
                "id": "worker",
                "org": "org",
                "type": "http",
                "endpoint": endpoint,
                "scope": "org",
                "owner": "org",
            }
        ),
    )
    change(
        path,
        "UPDATE agent_tool_reviews SET document = ? WHERE resource = ?",
        (json.dumps(review), f"mcp:worker/{name}"),
    )
    return tool


def compose(path: Path, operation: str):
    binding = original(path)
    checks = []

    async def actual_grant_fixture(registered, name):
        assert registered is binding
        assert registered.owner_id != "actor"
        assert name == operation
        checks.append((registered, name))

    resolvers = ReviewedModelResolvers(
        reviews=RetainedReviewedMcpRepo(path),
        registry=RetainedOntologyBindingRepo(path),
        actor_member_id="actor",
        check_access=actual_grant_fixture,
    )
    return (
        binding,
        RegisteredModelTransport(
            resolve_tool=resolvers.resolve_tool,
            resolve_review=resolvers.resolve_review,
        ),
        checks,
    )


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_original_sql_tuple_uses_official_mcp_and_one_original_call(
    reviewed: Path, operation: str
):
    content = [{"type": "text", "text": json.dumps({"revision": "a" * 64})}]
    state = {"result": {"content": content, "isError": False}}
    async with server(reviewed_server(state)) as (port, requests):
        state["tool"] = configure(reviewed, f"http://127.0.0.1:{port}/mcp", operation)
        binding, transport, checks = compose(reviewed, operation)
        reply = await transport.call_tool(binding, operation, OPERATIONS[operation])
        assert reply.content == content
        assert reply.is_error is False
        assert state["calls"] == [{"name": operation, "arguments": OPERATIONS[operation]}]
        assert checks and all(item[0] is binding for item in checks)
        assert requests[-1]["line"] == "DELETE /mcp HTTP/1.1"


@pytest.mark.parametrize("path", ["/mcp", "/nested/mcp"])
async def test_review_tuple_uses_independent_grant_and_original_relative_url(
    reviewed: Path, path: str
):
    payload = {"format": REVIEW_PROFILE, "result": {"html": "<html>original</html>"}}
    async with server(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, requests):
        configure(reviewed, f"http://127.0.0.1:{port}{path}", "sudograph_context_read")
        binding, transport, checks = compose(reviewed, "review")
        request = {"operation": "head", "resource": binding.project_id}
        assert await transport.read_review(binding, request) == payload
        assert len(requests) == 1
        assert requests[0]["line"] == f"POST {path.removesuffix('mcp')}review HTTP/1.1"
        assert json.loads(requests[0]["body"]) == request
        assert checks and all(item[1] == "review" for item in checks)


@pytest.mark.parametrize("after_effect", [False, True])
async def test_original_member_revocation_stops_dispatch_or_preserves_one_unknown_write(
    reviewed: Path, after_effect: bool
):
    state = {}
    respond = reviewed_server(state)

    def revoke(request):
        reply = respond(request)
        if request["line"].startswith("POST"):
            method = json.loads(request["body"]).get("method")
            if method == ("tools/call" if after_effect else "tools/list"):
                change(reviewed, "UPDATE users SET status = 'disabled' WHERE id = 'actor'")
        return reply

    async with server(revoke) as (port, _):
        state["tool"] = configure(reviewed, f"http://127.0.0.1:{port}/mcp", "sudograph_model_apply")
        binding, transport, _ = compose(reviewed, "sudograph_model_apply")
        with pytest.raises(
            (FixedJsonError, ToolProtocolError, PermissionError, SudographError)
        ) as e:
            await transport.call_tool(
                binding, "sudograph_model_apply", OPERATIONS["sudograph_model_apply"]
            )
        assert len(state.get("calls", [])) == (1 if after_effect else 0)
        if after_effect:
            assert isinstance(e.value, SudographError)
            assert e.value.code == "MODEL_WRITE_UNCONFIRMED"
            assert "original operation_id" in e.value.recovery
