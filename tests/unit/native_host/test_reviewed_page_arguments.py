"""Host review retains the exact independently reviewed read argument limits."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from tests.unit.db.test_retained_cohost import retained as retained
from tests.unit.db.test_retained_ontology import registered as registered
from tests.unit.native_host.test_reviewed_mcp import (
    Grants,
    change,
    original,
    resolver,
    set_review,
)
from tests.unit.native_host.test_reviewed_mcp import (
    reviewed as reviewed,
)

from octop.infra.connectors.tool_protocol import ToolProtocolError, content_digest


def _set_read(path: Path, *, records: bool = True, limit: int = 1, **schema_changes) -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "records": {"type": "boolean", "enum": [records]},
            "limit": {"type": "integer", "minimum": 1, "maximum": limit},
        },
    }
    schema.update(schema_changes)
    tool = {
        "name": "sudograph_context_read",
        "inputSchema": schema,
        "_meta": {"moss/host-review-profile": "sudograph-worker-review/v2"},
    }
    set_review(
        path,
        "sudograph_context_read",
        tool=tool,
        digest=content_digest({"protocol": "2025-03-26", "tool": tool}),
    )


def _page(limit: int = 1):
    return {
        "resource": "project-original",
        "operation": "page",
        "params": {"id": "sg-source/source-a/object/widgets", "limit": limit},
    }


@pytest.mark.parametrize("records,limit", [(False, 1), (True, 2)])
async def test_page_must_not_exceed_reviewed_record_or_row_permission(
    reviewed: Path, records: bool, limit: int
) -> None:
    _set_read(reviewed, records=records)
    with pytest.raises(ToolProtocolError, match="INVALID_TOOL_ARGUMENTS"):
        await resolver(reviewed, Grants()).resolve_review(original(reviewed), _page(limit))


async def test_original_approved_page_and_current_closure_remain_usable(reviewed: Path) -> None:
    _set_read(reviewed)
    grants = Grants()
    request = _page()
    before = copy.deepcopy(request)
    _, current = await resolver(reviewed, grants).resolve_review(original(reviewed), request)
    assert await current() is None
    assert request == before
    grants.allowed = False
    with pytest.raises(PermissionError):
        await current()


async def test_nonpage_review_does_not_invent_required_invocation_values(reviewed: Path) -> None:
    _set_read(reviewed, required=["id"])
    _, current = await resolver(reviewed, Grants()).resolve_review(
        original(reviewed), {"resource": "project-original", "operation": "head"}
    )
    assert await current() is None


async def test_nonpage_review_still_requires_the_supported_reviewed_schema(reviewed: Path) -> None:
    _set_read(reviewed, additionalProperties=True)
    with pytest.raises(ToolProtocolError, match="TOOL_SCHEMA_UNSUPPORTED"):
        await resolver(reviewed, Grants()).resolve_review(
            original(reviewed), {"resource": "project-original", "operation": "head"}
        )


async def test_independent_full_model_denial_stays_denied(reviewed: Path) -> None:
    _set_read(reviewed)
    grants = Grants()
    grants.allowed = False
    with pytest.raises(PermissionError):
        await resolver(reviewed, grants).resolve_review(original(reviewed), _page())


@pytest.mark.parametrize("initial_limit,later_limit,allowed", [(1, 2, True), (2, 1, False)])
async def test_page_arguments_are_captured_before_authority_waits(
    reviewed: Path, initial_limit: int, later_limit: int, allowed: bool
) -> None:
    _set_read(reviewed)
    request = _page(initial_limit)

    async def grant_wait(*_):
        request["params"]["limit"] = later_limit

    producer = resolver(reviewed, grant_wait)
    if allowed:
        _, current = await producer.resolve_review(original(reviewed), request)
        assert await current() is None
    else:
        with pytest.raises(ToolProtocolError, match="INVALID_TOOL_ARGUMENTS"):
            await producer.resolve_review(original(reviewed), request)


@pytest.mark.parametrize(
    "records,limit,allowed", [(False, 1, False), (True, 2, False), (True, 1, True)]
)
async def test_review_argument_permission_precedes_secret_capability_use(
    reviewed: Path, records: bool, limit: int, allowed: bool
) -> None:
    _set_read(reviewed, records=records)
    change(
        reviewed,
        "UPDATE mcp_servers SET auth_type='secret_ref',secret_ref='worker-secret'",
    )
    calls = []

    async def secrets(namespace, subject):
        calls.append((namespace, subject))
        return [{"key": "token", "value": "synthetic-secret"}]

    producer = resolver(reviewed, Grants(), list_secrets=secrets)
    if allowed:
        _, current = await producer.resolve_review(original(reviewed), _page(limit))
        assert await current() is None
        assert calls
    else:
        with pytest.raises(ToolProtocolError, match="INVALID_TOOL_ARGUMENTS"):
            await producer.resolve_review(original(reviewed), _page(limit))
        assert calls == []
