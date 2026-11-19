from unittest.mock import AsyncMock

import pytest

from octop.infra.company_runtime.runtime import _load_connector_tools


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
