import logging
from datetime import UTC
from datetime import datetime as RealDateTime
from unittest.mock import AsyncMock

import pytest

import octop.infra.company_runtime.runtime as runtime_module
from octop.infra.company_runtime.client import CompanyServiceError
from octop.infra.company_runtime.runtime import _attach_octop_file_logging, _load_connector_tools


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
