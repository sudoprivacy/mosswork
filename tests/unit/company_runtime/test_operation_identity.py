import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessageChunk
from langchain_core.messages.tool import tool_call

from octop.infra.company_runtime.client import RemoteCheckpointSaver
from octop.infra.company_runtime.local import OperationJournal, ReceiptMiddleware
from octop.infra.company_runtime.processes import _TOOL_CALL_ID

from .test_resilience import run_turn, runtime_for
from .test_runtime import ToolModel, install_agent


async def test_reused_provider_call_id_runs_new_steps_in_same_thread(company, tmp_path):
    server, client, _, _ = company
    install_agent(server)
    steps = iter(["one", "two", None, "three", None])
    call_id = "functions.write_file:21"

    class ReusingModel(ToolModel):
        async def astream(self, messages):
            name = next(steps)
            if name is None:
                yield AIMessageChunk(content="Done")
            else:
                yield AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "write_file",
                            "args": json.dumps({"file_path": f"/{name}.txt", "content": name}),
                            "id": call_id,
                            "index": 0,
                        }
                    ],
                )

    server.app_runtime.agent_registry.get_agent("expert").model_factory.get_chat_model = (
        lambda ref: ReusingModel()
    )
    runtime, events = runtime_for(client, tmp_path)
    journal = OperationJournal(runtime.state / "threads/thread1/operations")
    journal.save(call_id, request_hash="legacy", status="unknown", tool="write_file")
    try:
        for task_id in ("first", "second"):
            await run_turn(runtime, task_id=task_id)
            assert runtime.tasks["thread1"]["status"] == "completed", events[-1]
        for name in ("one", "two", "three"):
            assert (runtime.workspace / "thread1" / f"{name}.txt").read_text() == name
        assert journal.get(call_id)["status"] == "unknown"
        saver = RemoteCheckpointSaver(client, "second", "thread1")
        checkpoint = await saver.aget_tuple({"configurable": {"thread_id": "thread1"}})
        state = checkpoint.checkpoint["channel_values"]
        original = tool_call(
            name="write_file", args={"file_path": "/three.txt", "content": "three"}, id=call_id
        )
        handler = AsyncMock()
        await ReceiptMiddleware(journal, saver, AsyncMock()).awrap_tool_call(
            SimpleNamespace(state=state, tool_call=original), handler
        )
        handler.assert_not_awaited()
    finally:
        await runtime.close()


@pytest.mark.parametrize("scope", [None, "model-step"])
async def test_operation_replay_and_changed_arguments_after_restart(tmp_path, scope):
    journal = OperationJournal(tmp_path)
    state = {} if scope is None else {"company_receipt_scope": scope}
    request = SimpleNamespace(
        state=state, tool_call={"id": "call", "name": "execute", "args": {"command": "first"}}
    )
    saver = SimpleNamespace(pack=str, unpack=str)
    authorize = AsyncMock()

    async def execute(request):
        assert _TOOL_CALL_ID.get() == "call"
        return "original result"

    handler = AsyncMock(side_effect=execute)
    middleware = ReceiptMiddleware(journal, saver, authorize)
    assert await asyncio.gather(
        *(middleware.awrap_tool_call(request, handler) for _ in range(2))
    ) == ["original result", "original result"]
    restored = ReceiptMiddleware(OperationJournal(tmp_path), saver, authorize)
    assert await restored.awrap_tool_call(request, handler) == "original result"
    request.tool_call["args"]["command"] = "changed"
    with pytest.raises(RuntimeError, match="operation_identity_conflict"):
        await restored.awrap_tool_call(request, handler)
    handler.assert_awaited_once()
    authorize.assert_awaited_once()


async def test_identical_requests_in_new_steps_do_not_reuse_old_results(tmp_path):
    middleware = ReceiptMiddleware(
        OperationJournal(tmp_path), SimpleNamespace(pack=str, unpack=str), AsyncMock()
    )
    call = {"id": "call", "name": "read_file", "args": {"file_path": "/changing.txt"}}
    handler = AsyncMock(side_effect=["before", "after"])
    for scope, result in (("first", "before"), ("second", "after")):
        request = SimpleNamespace(tool_call=call, state={"company_receipt_scope": scope})
        assert await middleware.awrap_tool_call(request, handler) == result
    assert handler.await_count == 2


async def test_interrupted_scoped_operation_is_not_retried(tmp_path):
    request = SimpleNamespace(
        tool_call={"id": "call", "name": "execute", "args": {"command": "first"}},
        state={"company_receipt_scope": "model-step"},
    )
    handler = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await ReceiptMiddleware(OperationJournal(tmp_path), None, AsyncMock()).awrap_tool_call(
            request, handler
        )
    with pytest.raises(RuntimeError, match="operation_result_unknown"):
        await ReceiptMiddleware(OperationJournal(tmp_path), None, AsyncMock()).awrap_tool_call(
            request, handler
        )
    handler.assert_awaited_once()


@pytest.mark.parametrize("status", ["running", "unknown"])
async def test_legacy_checkpoint_with_unconfirmed_receipt_does_not_reexecute(tmp_path, status):
    call = {"id": "call", "name": "execute", "args": {"command": "first"}}
    digest = hashlib.sha256(
        json.dumps(call, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    legacy_path = tmp_path / (hashlib.sha256(b"call").hexdigest() + ".json")
    legacy_path.write_text(
        json.dumps({"operation_id": "call", "status": status, "request_hash": digest})
    )
    middleware = ReceiptMiddleware(OperationJournal(tmp_path), None, AsyncMock())
    handler = AsyncMock()
    with pytest.raises(RuntimeError, match="operation_result_unknown"):
        await middleware.awrap_tool_call(SimpleNamespace(tool_call=call, state={}), handler)
    handler.assert_not_awaited()
