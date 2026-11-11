"""Trust, connection lifetime and deadline boundaries of ACP observations."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from moss_harness.acp.client import ACPHostedClient
from moss_harness.acp.models import ACPConfig, ACPRunnerConfig
from moss_harness.acp.runtime_status import supports_runtime_status
from moss_harness.acp.service import ACPService, _Conversation


def snapshot(state: str = "idle", **foreground: str) -> dict[str, Any]:
    return {
        "foreground": {"state": state, **foreground},
        "background": {
            "terminals": 2,
            "agents": 1,
            "execution_unconfirmed": 0,
            "tool_operations": 3,
        },
    }


def bind(service: ACPService, runner: str = "custom", *, supported: bool = True) -> _Conversation:
    config = ACPRunnerConfig(enabled=True)
    conversation = _Conversation(
        thread_id="thread",
        runner=runner,
        acp_session_id="session-" + runner,
        cwd="",
        conn=SimpleNamespace(ext_method=AsyncMock(return_value=snapshot())),
        process=SimpleNamespace(returncode=None),
        client=ACPHostedClient(runner_name=runner, runner_config=config, cwd=""),
        exit_stack=AsyncExitStack(),
        turn_lock=asyncio.Lock(),
        runtime_status_supported=supported,
    )
    service._sessions[("thread", runner)] = conversation
    return conversation


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (None, False),
        ({}, False),
        ({"sudocode": "custom"}, False),
        ({"sudocode": {"runtimeStatus": True}}, False),
        ({"sudocode": {"runtimeStatus": {"version": True}}}, False),
        ({"sudocode": {"runtimeStatus": {"version": "1"}}}, False),
        ({"sudocode": {"runtimeStatus": {"version": 2}}}, False),
        ({"sudocode": {"runtimeStatus": {"version": 1}}}, True),
    ],
)
def test_capability_requires_an_exact_negotiated_version(
    metadata: dict[str, Any] | None, expected: bool
) -> None:
    assert supports_runtime_status(metadata) is expected


async def test_unknown_connections_never_spawn_or_query_an_unsupported_backend() -> None:
    service = ACPService(config=ACPConfig())
    absent = await service.get_runtime_observation(thread_id="thread", runner="custom")
    assert absent.availability == "disconnected"
    assert absent.snapshot is None
    assert service._sessions == {}
    conversation = bind(service, supported=False)
    unsupported = await service.get_runtime_observation(thread_id="thread", runner="custom")
    assert unsupported.availability == "unsupported"
    assert unsupported.snapshot is None
    conversation.conn.ext_method.assert_not_awaited()
    conversation.process.returncode = 0
    assert (
        await service.get_runtime_observation(thread_id="thread", runner="custom")
    ).availability == "disconnected"
    conversation.conn.ext_method.assert_not_awaited()


async def test_input_wait_and_background_work_survive_a_busy_turn_lock() -> None:
    service = ACPService(config=ACPConfig())
    conversation = bind(service)
    conversation.conn.ext_method.return_value = snapshot("awaiting_input", waiting_for="question")
    async with conversation.turn_lock:
        async with asyncio.timeout(2):
            observation = await service.get_runtime_observation(thread_id="thread", runner="custom")
    assert observation.availability == "fresh"
    assert observation.scope == "moss-owned-acp-connections"
    assert observation.is_complete is False
    assert observation.snapshot is not None
    assert observation.snapshot.foreground.model_dump() == {
        "state": "awaiting_input",
        "waiting_for": "question",
    }
    assert observation.snapshot.background.model_dump() == snapshot()["background"]
    conversation.conn.ext_method.assert_awaited_once_with(
        "sudocode/session/status", {"sessionId": "session-custom"}
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {**snapshot(), "foreground": {"state": "awaiting_input"}},
        {**snapshot(), "foreground": {"state": "awaiting_input", "waiting_for": "unknown"}},
        {**snapshot(), "foreground": {"state": "idle", "waiting_for": "permission"}},
        {**snapshot(), "background": {**snapshot()["background"], "agents": -1}},
        {**snapshot(), "background": {**snapshot()["background"], "agents": True}},
        {**snapshot(), "background": {**snapshot()["background"], "agents": "1"}},
    ],
)
async def test_malformed_engine_facts_are_unknown_instead_of_idle(payload: dict[str, Any]) -> None:
    service = ACPService(config=ACPConfig())
    conversation = bind(service)
    conversation.conn.ext_method.return_value = payload
    observation = await service.get_runtime_observation(thread_id="thread", runner="custom")
    assert observation.availability == "unavailable"
    assert observation.snapshot is None


async def test_late_reply_cannot_describe_a_replacement_connection() -> None:
    service = ACPService(config=ACPConfig())
    original = bind(service)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_reply(*_: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return snapshot("running")

    original.conn.ext_method.side_effect = delayed_reply
    pending = asyncio.create_task(
        service.get_runtime_observation(thread_id="thread", runner="custom")
    )
    async with asyncio.timeout(2):
        await entered.wait()
        replacement = bind(service)
        replacement.acp_session_id = original.acp_session_id
        release.set()
        stale = await pending
    assert stale.availability == "disconnected"
    assert stale.snapshot is None
    assert stale.instance_id == original.instance_id
    fresh = await service.get_runtime_observation(thread_id="thread", runner="custom")
    assert fresh.availability == "fresh"
    assert fresh.instance_id == replacement.instance_id != original.instance_id
    assert fresh.session_id == original.acp_session_id


async def test_batch_has_one_deadline_and_does_not_send_expired_queued_reads() -> None:
    service = ACPService(config=ACPConfig())
    runners = [f"runner-{index}" for index in range(12)]
    active = 0
    started: list[str] = []
    finished: list[str] = []
    release = asyncio.Event()

    async def delayed_reply(runner: str, *_: Any) -> dict[str, Any]:
        nonlocal active
        active += 1
        started.append(runner)
        try:
            await release.wait()
            return snapshot()
        finally:
            active -= 1
            finished.append(runner)

    for runner in runners:
        conversation = bind(service, runner)

        async def query(*args: Any, name: str = runner) -> dict[str, Any]:
            return await delayed_reply(name, *args)

        conversation.conn.ext_method.side_effect = query
    async with asyncio.timeout(10):
        observations = await service.get_runtime_observations(thread_id="thread", runners=runners)
    assert list(observations) == runners
    assert all(
        item.availability == "unavailable" and item.snapshot is None
        for item in observations.values()
    )
    assert 0 < len(started) <= 4
    first_started = list(started)
    assert finished == []
    again = await service.get_runtime_observations(thread_id="thread", runners=first_started)
    assert all(item.availability == "unavailable" for item in again.values())
    assert started == first_started, (
        "Repeated reads must not accumulate wire requests on stalled connections"
    )
    release.set()
    await asyncio.gather(
        *(
            item.runtime_status_task
            for item in service._sessions.values()
            if item.runtime_status_task is not None
        )
    )
    recovered = await service.get_runtime_observations(thread_id="thread", runners=runners)
    assert all(
        item.availability == "fresh" and item.snapshot is not None for item in recovered.values()
    )
    assert active == 0


async def test_caller_cancellation_is_not_converted_into_an_engine_fact() -> None:
    service = ACPService(config=ACPConfig())
    conversation = bind(service)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_reply(*_: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return snapshot()

    conversation.conn.ext_method.side_effect = delayed_reply
    pending = asyncio.create_task(
        service.get_runtime_observation(thread_id="thread", runner="custom")
    )
    async with asyncio.timeout(2):
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert conversation.runtime_status_task is not None
        assert not conversation.runtime_status_task.cancelled()
        other_reader = asyncio.create_task(
            service.get_runtime_observation(thread_id="thread", runner="custom")
        )
        release.set()
        assert (await other_reader).availability == "fresh"
    conversation.conn.ext_method.assert_awaited_once()


async def test_runner_list_and_status_use_the_same_scoped_engine_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from moss_harness.builtin.tools import acp_runner as runner_tool

    config = ACPConfig(runners={"custom": ACPRunnerConfig(enabled=True)})
    service = ACPService(config=config)
    conversation = bind(service)
    conversation.conn.ext_method.return_value = snapshot("awaiting_input", waiting_for="permission")
    monkeypatch.setattr(runner_tool, "_thread_id_from_config", lambda: "thread")
    monkeypatch.setattr(runner_tool, "_get_service", lambda *_: service)
    listed = await runner_tool._format_runner_list(config, "agent")
    status = await runner_tool._format_status(config, "agent", "custom")
    listed_payload = json.loads(listed.split("runtime: ", 1)[1].rsplit(")", 1)[0])
    status_payload = json.loads(status.split("runtime: ", 1)[1])
    assert listed_payload == status_payload
    assert status_payload["snapshot"]["foreground"] == {
        "state": "awaiting_input",
        "waiting_for": "permission",
    }
    assert status_payload["is_complete"] is False


async def test_closing_a_connection_releases_its_outstanding_status_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from moss_harness.acp import service as service_module

    service = ACPService(config=ACPConfig())
    conversation = bind(service)
    conversation.process.pid = 42
    conversation.conn.close_session = AsyncMock(return_value=None)
    closed: list[int] = []
    monkeypatch.setattr(service_module, "_kill_process_tree", closed.append)
    entered = asyncio.Event()

    async def never_replies(*_: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    conversation.conn.ext_method.side_effect = never_replies
    reader = asyncio.create_task(
        service.get_runtime_observation(thread_id="thread", runner="custom")
    )
    async with asyncio.timeout(10):
        await entered.wait()
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        await service.close_thread_session(thread_id="thread", runner="custom")
    assert conversation.runtime_status_task is not None
    assert conversation.runtime_status_task.cancelled()
    assert closed == [42]
    observation = await service.get_runtime_observation(thread_id="thread", runner="custom")
    assert observation.availability == "disconnected"
    assert observation.snapshot is None
