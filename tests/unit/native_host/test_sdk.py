"""IPC refusal/race/unknown regressions; the subprocess is an explicit fixture."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from octop.infra.native_host.sdk import (
    ControllerSdkCommand,
    ControllerSdkConfiguration,
    ControllerSdkConnection,
    ControllerSdkError,
)


async def allowed(*_args):
    return None


def command():
    return ControllerSdkCommand(
        Path(sys.executable).resolve(), Path(__file__).parent / "fixtures/controller_stub.py"
    )


def config(tmp_path, scenario="echo"):
    return ControllerSdkConfiguration(
        owner_id="canonical-original-owner",
        issuer_endpoint="https://issuer.invalid",
        ca_file=tmp_path / "ca.pem",
        cert_file=tmp_path / "controller.pem",
        key_file=tmp_path / "controller-key.pem",
        runtime_endpoint="https://owner-runtime.invalid",
        server_name=scenario,
    )


async def connect(tmp_path, scenario="echo", **kwargs):
    return await ControllerSdkConnection.connect(
        command(),
        config(tmp_path, scenario),
        before_connect=allowed,
        before_dispatch=allowed,
        **kwargs,
    )


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def calls(tmp_path):
    path = tmp_path / "calls.jsonl"
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )


@pytest.mark.asyncio
async def test_private_controller_call_preserves_payload_and_returns_original_string(tmp_path):
    sdk = await connect(tmp_path)
    try:
        payload = json.dumps({"unicode": "客户 📝\noriginal"}, ensure_ascii=False)
        assert await sdk.call("managed_agent.get_session_v1", payload, "") == payload
        assert calls(tmp_path) == [
            {"type": "call", "id": 1, "method": "managed_agent.get_session_v1", "payload": payload}
        ]
    finally:
        await sdk.close()
    assert sdk._process.returncode == 0
    assert not sdk._pending


@pytest.mark.parametrize("verdict", [False, True, {}, "allow"])
@pytest.mark.asyncio
async def test_nonasserting_connect_authority_refuses_before_process_or_credential_access(
    tmp_path, monkeypatch, verdict
):
    spawn = AsyncMock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def invalid():
        return verdict

    with pytest.raises(ControllerSdkError, match="CONNECT_AUTHORIZATION_UNAVAILABLE"):
        await ControllerSdkConnection.connect(
            command(), config(tmp_path), before_connect=invalid, before_dispatch=allowed
        )
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_connect_revoked_during_boot_reaps_component_without_runtime_calls(tmp_path):
    checked = 0

    async def check():
        nonlocal checked
        checked += 1
        if checked == 2:
            raise PermissionError("private canonical denial")

    with pytest.raises(PermissionError, match="connection access denied") as failure:
        await ControllerSdkConnection.connect(
            command(), config(tmp_path), before_connect=check, before_dispatch=allowed
        )
    assert "private canonical denial" not in str(failure.value)
    assert checked == 2
    assert calls(tmp_path) == []


@pytest.mark.parametrize("verdict", [False, True, {}, "allow"])
@pytest.mark.asyncio
async def test_nonasserting_dispatch_never_writes_request(tmp_path, verdict):
    async def invalid(*_args):
        return verdict

    sdk = await ControllerSdkConnection.connect(
        command(), config(tmp_path), before_connect=allowed, before_dispatch=invalid
    )
    try:
        with pytest.raises(ControllerSdkError, match="DISPATCH_UNAVAILABLE") as error:
            await sdk.call("managed_agent.start_session_v1", "{}", "")
        assert error.value.is_outcome_uncertain is False
        assert calls(tmp_path) == []
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_current_grant_is_rechecked_after_capacity_wait_before_dispatch(tmp_path):
    is_granted = True

    async def current(*_args):
        if not is_granted:
            raise PermissionError("revoked")

    sdk = await ControllerSdkConnection.connect(
        command(), config(tmp_path, "held-batch"), before_connect=allowed, before_dispatch=current
    )
    tasks = [asyncio.create_task(sdk.call("agent_list", str(index), "")) for index in range(16)]
    try:
        await wait_until(lambda: (tmp_path / "held").exists())
        waiting = asyncio.create_task(sdk.call("managed_agent.start_session_v1", "{}", ""))
        await asyncio.sleep(0)
        assert not waiting.done()
        is_granted = False
        (tmp_path / "release").touch()
        assert await asyncio.gather(*tasks) == [str(index) for index in range(16)]
        with pytest.raises(PermissionError, match="operation denied"):
            await waiting
        assert len(calls(tmp_path)) == 16
    finally:
        (tmp_path / "release").touch()
        await sdk.close()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    "scenario", ["lost", "malformed", "foreign-id", "duplicate-field", "bad-certain"]
)
@pytest.mark.asyncio
async def test_lost_or_invalid_reply_stays_unknown_without_retry_or_rebinding(tmp_path, scenario):
    sdk = await connect(tmp_path, scenario)
    try:
        with pytest.raises(ControllerSdkError, match="OUTCOME_UNKNOWN") as failure:
            await sdk.call("managed_agent.start_session_v1", "{}", "")
        assert failure.value.is_outcome_uncertain is True
        with pytest.raises(ControllerSdkError, match="CONNECTION_UNAVAILABLE"):
            await sdk.call("managed_agent.start_session_v1", "{}", "")
        assert len(calls(tmp_path)) == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_native_structured_error_is_controller_only_and_not_discarded(tmp_path):
    sdk = await connect(tmp_path, "native-error")
    try:
        with pytest.raises(ControllerSdkError) as failure:
            await sdk.call("managed_agent.get_session_v1", "{}", "")
        assert failure.value.code == "SDK_CALL_FAILED"
        assert failure.value.is_outcome_uncertain
        assert "private" not in repr(failure.value)
        assert failure.value.native_failure == {
            "code": -32603,
            "message": "private /original/repository",
        }
        copy = failure.value.native_failure
        copy["message"] = "changed"
        assert failure.value.native_failure["message"] == "private /original/repository"
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_rpc_timeout_keeps_pending_effect_until_original_reply_without_replay(tmp_path):
    sdk = await connect(tmp_path, "delay", rpc_timeout_seconds=0.02)
    try:
        with pytest.raises(ControllerSdkError, match="OUTCOME_UNKNOWN"):
            await sdk.call("managed_agent.start_session_v1", "{}", "")
        assert len(calls(tmp_path)) == 1
        await wait_until(lambda: not sdk._pending)
        assert len(calls(tmp_path)) == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_or_repeat_native_request(tmp_path):
    sdk = await connect(tmp_path, "delay")
    try:
        task = asyncio.create_task(sdk.call("managed_agent.start_session_v1", "{}", ""))
        await wait_until(lambda: bool(calls(tmp_path)))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await wait_until(lambda: not sdk._pending)
        assert len(calls(tmp_path)) == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_runtime_credentials_and_node_injection_environment_are_not_inherited(
    tmp_path, monkeypatch
):
    for name in ("MOSSWORK_TEST_PRIVATE_KEY", "NODE_OPTIONS", "NODE_PATH", "PYTHONPATH"):
        monkeypatch.setenv(name, "private-test-value")
    sdk = await connect(tmp_path)
    try:
        result = json.loads(await sdk.call("environment", "{}", ""))
        assert result == dict.fromkeys(
            ("MOSSWORK_TEST_PRIVATE_KEY", "NODE_OPTIONS", "NODE_PATH", "PYTHONPATH"), False
        )
        with pytest.raises(ValueError, match="cannot override"):
            await sdk.call("agent_list", "{}", "deployment-token")
        assert len(calls(tmp_path)) == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_bool_protocol_version_does_not_open_controller(tmp_path):
    with pytest.raises(ControllerSdkError, match="CONNECT_UNAVAILABLE"):
        await connect(tmp_path, "bool-ready")
    assert calls(tmp_path) == []


@pytest.mark.asyncio
async def test_cancellation_during_process_creation_reaps_only_the_owned_component(
    tmp_path, monkeypatch
):
    original = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        entered.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(connect(tmp_path))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(processes) == 1 and processes[0].returncode == 0
    assert calls(tmp_path) == []
