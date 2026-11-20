import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, messages_to_dict

from octop.infra.company_runtime import client as client_module
from octop.infra.company_runtime.client import CompanyChatModel, CompanyClient, CompanyModelError
from octop.infra.company_runtime.model_retry import EmittingModelRetryMiddleware


class _DelayedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[tuple[float, bytes]]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for delay, chunk in self.chunks:
            await asyncio.sleep(delay)
            yield chunk


async def test_lease_rotation_preserves_open_stream_and_changes_next_request():
    seen = []

    async def handle(request):
        seen.append(request.headers["X-mosswork-Device-Credential"])
        return httpx.Response(200, stream=_DelayedStream([(0, b'{"ok":true}')]))

    client = CompanyClient("http://company.test", "old", transport=httpx.MockTransport(handle))
    try:
        async with client.http.stream("POST", "tasks/id/model") as response:
            client.replace_credential("mosswork-device.new")
            assert await client.request("tasks/id/checkpoint", {}) == {"ok": True}
            assert await response.aread() == b'{"ok":true}'
        assert seen == ["old", "mosswork-device.new"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_first_byte_timeout_is_retryable(monkeypatch):
    monkeypatch.setattr(client_module, "_FIRST_BYTE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(client_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.005)

    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_DelayedStream([(1.0, b"never\n")]),
            headers={"content-type": "application/x-ndjson"},
        )

    company = CompanyClient(
        "http://company.test", "credential", transport=httpx.MockTransport(handle)
    )
    model = CompanyChatModel(client=company, task_id="task", model_ref="test/model")
    try:
        with pytest.raises(CompanyModelError, match="company_model_timeout") as raised:
            async for _ in model.astream([HumanMessage(content="hello")]):
                pass
        assert raised.value.retryable
    finally:
        await company.close()


@pytest.mark.asyncio
async def test_heartbeat_keeps_reader_alive_and_reports_waiting(monkeypatch):
    monkeypatch.setattr(client_module, "_FIRST_BYTE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(client_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    first = json.dumps(messages_to_dict([AIMessageChunk(content="first")])[0]).encode() + b"\n"
    second = json.dumps(messages_to_dict([AIMessageChunk(content="second")])[0]).encode() + b"\n"
    progress: list[dict[str, str]] = []

    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_DelayedStream([(0, first), (0.03, second)]),
            headers={"content-type": "application/x-ndjson"},
        )

    company = CompanyClient(
        "http://company.test", "credential", transport=httpx.MockTransport(handle)
    )
    model = CompanyChatModel(
        client=company,
        task_id="task",
        model_ref="test/model",
        progress=progress.append,
    )
    try:
        chunks = [chunk async for chunk in model.astream([HumanMessage(content="hello")])]
        assert [chunk.content for chunk in chunks if chunk.content] == ["first", "second"]
        assert {item["status"] for item in progress} == {"waiting"}
    finally:
        await company.close()


@pytest.mark.asyncio
async def test_retry_middleware_emits_before_each_retry():
    errors: list[tuple[str, int, int]] = []
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise CompanyModelError("company_model_timeout", retryable=True)
        return "ok"

    middleware = EmittingModelRetryMiddleware(
        max_retries=3,
        initial_delay=0,
        jitter=False,
        retry_on=lambda exc: isinstance(exc, CompanyModelError) and exc.retryable,
        on_failure="error",
        on_retry=lambda exc, attempt, total: errors.append((str(exc), attempt, total)),
    )
    assert await middleware.awrap_model_call(object(), handler) == "ok"
    assert calls == 3
    assert errors == [
        ("company_model_timeout", 1, 3),
        ("company_model_timeout", 2, 3),
    ]
