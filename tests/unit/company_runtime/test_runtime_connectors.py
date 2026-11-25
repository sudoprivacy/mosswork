import logging
from datetime import UTC
from datetime import datetime as RealDateTime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

import octop.infra.company_runtime.runtime as runtime_module
from octop.infra.company_runtime.client import CompanyServiceError
from octop.infra.company_runtime.local import ReceiptMiddleware
from octop.infra.company_runtime.runtime import (
    _attach_octop_file_logging,
    _load_connector_tools,
    _resolve_connector_descriptor,
)


@pytest.fixture(autouse=True)
def cleanup_octop_file_handler():
    logger = logging.getLogger("octop")
    for handler in list(logger.handlers):
        if getattr(handler, "name", None) == "octop_company_file_handler":
            handler.close()
            logger.removeHandler(handler)
    yield
    for handler in list(logger.handlers):
        if getattr(handler, "name", None) == "octop_company_file_handler":
            handler.close()
            logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_connector_tool_discovery_failure_does_not_block_task_start():
    client = type("Client", (), {"request": AsyncMock(side_effect=RuntimeError("offline"))})()

    assert await _load_connector_tools(client, "task1", ["qq-mail__one"]) == []


@pytest.mark.asyncio
async def test_connector_tool_discovery_keeps_valid_list():
    client = type("Client", (), {"request": AsyncMock(return_value=[{"tool_name": "search"}])})()

    assert await _load_connector_tools(client, "task1", ["qq-mail__one"]) == [
        {"tool_name": "search"}
    ]


@pytest.mark.asyncio
async def test_connector_tool_discovery_warning_is_written_to_octop_log(tmp_path):
    log_dir = tmp_path / "logs"
    _attach_octop_file_logging(log_dir)
    client = type(
        "Client",
        (),
        {"request": AsyncMock(side_effect=CompanyServiceError("denied", 403))},
    )()

    assert await _load_connector_tools(client, "task1", ["feishu__one"]) == []

    log_file = next(log_dir.glob("octop-*.log"))
    content = log_file.read_text(encoding="utf-8")
    assert "company connector tool discovery failed" in content
    assert "403" in content


def test_octop_file_handler_is_idempotent_and_switches_files(tmp_path, monkeypatch):
    logger = logging.getLogger("octop")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    _attach_octop_file_logging(first_dir)
    first_handler = next(
        handler
        for handler in logger.handlers
        if getattr(handler, "name", None) == "octop_company_file_handler"
    )
    _attach_octop_file_logging(first_dir)
    assert [
        handler
        for handler in logger.handlers
        if getattr(handler, "name", None) == "octop_company_file_handler"
    ] == [first_handler]

    _attach_octop_file_logging(second_dir)
    second_handler = next(
        handler
        for handler in logger.handlers
        if getattr(handler, "name", None) == "octop_company_file_handler"
    )
    assert second_handler is not first_handler
    assert first_handler not in logger.handlers

    class FixedDateTime:
        @classmethod
        def now(cls):
            return RealDateTime(2026, 1, 2, tzinfo=UTC)

    monkeypatch.setattr(runtime_module, "datetime", FixedDateTime)
    _attach_octop_file_logging(second_dir)
    third_handler = next(
        handler
        for handler in logger.handlers
        if getattr(handler, "name", None) == "octop_company_file_handler"
    )
    assert third_handler is not second_handler
    assert third_handler.baseFilename.endswith("octop-2026-01-02.log")


@pytest.mark.asyncio
async def test_receipt_middleware_keeps_connector_tool_for_model():
    middleware = ReceiptMiddleware(None, None, None)

    @tool
    async def company_connector_call(
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        tool_call_id: str = "",
    ) -> str:
        """Call a selected company connector tool.

        Read and external-query operations run directly. Operations
        that can change external state require explicit user approval.
        Never retry a call whose result is reported as unknown.
        """
        return "ok"

    @tool
    async def not_in_allowlist(query: str) -> str:
        """A stub tool that is not granted to this task."""
        return "x"

    async def handler(request):
        # moss-harness 0.0.8 middleware contract: the wrapped handler returns a
        # response object exposing ``.result`` (the emitted messages), not the
        # request itself. No AIMessage tool calls here, so the middleware takes
        # the passthrough branch and returns the response unchanged.
        return SimpleNamespace(tools=list(request.tools), result=[])

    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="hi")],
        tools=[company_connector_call, not_in_allowlist],
    )
    filtered = await middleware.awrap_model_call(request, handler)

    names = [item.name for item in filtered.tools]
    assert "company_connector_call" in names
    assert "not_in_allowlist" not in names


def test_receipt_middleware_allows_connector_tool_execution():
    # awrap_tool_call (local.py) rejects any call whose name is not in ALLOWED;
    # company_connector_call must pass the same allowlist it is shown to the model.
    assert "company_connector_call" in ReceiptMiddleware.ALLOWED


def _feishu_index() -> dict[tuple[str, str], dict[str, Any]]:
    return {
        ("feishu-cli__x", tool): {"server_name": "feishu-cli__x", "tool_name": tool}
        for tool in ("doc", "base", "calendar", "im", "help")
    }


def test_resolve_connector_descriptor_exact_match_returns_target():
    index = _feishu_index()
    assert _resolve_connector_descriptor(index, "feishu-cli__x", "doc") == (
        "feishu-cli__x",
        "doc",
        index[("feishu-cli__x", "doc")],
    )


def test_resolve_connector_descriptor_accepts_merged_doc_name():
    # Observed in task 9528f2b7/36b9ef9c: the model passed the combined
    # "server::tool" listing token as server_name.
    assert _resolve_connector_descriptor(_feishu_index(), "feishu-cli__x::doc", "doc") == (
        "feishu-cli__x",
        "doc",
        {"server_name": "feishu-cli__x", "tool_name": "doc"},
    )


def test_resolve_connector_descriptor_accepts_merged_help_name():
    assert _resolve_connector_descriptor(_feishu_index(), "feishu-cli__x::help", "help") == (
        "feishu-cli__x",
        "help",
        {"server_name": "feishu-cli__x", "tool_name": "help"},
    )


def test_resolve_connector_descriptor_falls_back_to_merged_tail_when_tool_empty():
    assert _resolve_connector_descriptor(_feishu_index(), "feishu-cli__x::doc", "") == (
        "feishu-cli__x",
        "doc",
        {"server_name": "feishu-cli__x", "tool_name": "doc"},
    )


def test_resolve_connector_descriptor_prefers_explicit_tool_over_merged_tail():
    resolved = _resolve_connector_descriptor(_feishu_index(), "feishu-cli__x::doc", "base")
    assert resolved is not None
    assert resolved[:2] == ("feishu-cli__x", "base")


def test_resolve_connector_descriptor_returns_none_for_unknown_server():
    index = {("other-srv", "doc"): {"server_name": "other-srv", "tool_name": "doc"}}
    assert _resolve_connector_descriptor(index, "feishu-cli__x::doc", "base") is None


def test_resolve_connector_descriptor_returns_none_without_separator():
    assert _resolve_connector_descriptor(_feishu_index(), "feishu-cli__x", "nope") is None
