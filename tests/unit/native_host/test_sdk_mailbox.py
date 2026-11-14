"""Private session IPC races; the controller here is an explicit fixture.

Actual SDK/mTLS/typed mailbox acceptance is in Sudostack's package tests.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from tests.unit.native_host.test_sdk import (
    allowed,
    calls,
    command,
    config,
    connect,
    wait_until,
)

from octop.infra.native_host.sdk import ControllerSdkConnection, ControllerSdkError


async def test_typed_mailbox_surface_shares_original_component_and_local_correlations(tmp_path):
    checked = []

    async def check(method, payload):
        checked.append((method, payload))

    sdk = await ControllerSdkConnection.connect(
        command(), config(tmp_path), before_connect=allowed, before_dispatch=check
    )
    try:
        assert await sdk.call("managed_agent.get_session_v1", "{}", "") == "{}"
        endpoint = json.dumps({"controller": "original", "text": "客户 🧩"}, ensure_ascii=False)
        assert await sdk.session_operation("open", endpoint) == endpoint
        message = '{"jsonrpc":"2.0","id":1,"error":{"code":-32002,"data":{"sessionId":"old"}}}'
        assert await sdk.session_operation("send", message) == message
        assert await sdk.session_operation("receive") == ""
        assert await sdk.session_operation("close") == ""
        assert [call["id"] for call in calls(tmp_path)] == [1, 2, 3, 4, 5]
        assert [call["type"] for call in calls(tmp_path)] == ["call"] + ["session"] * 4
        assert checked == [
            ("managed_agent.get_session_v1", "{}"),
            ("controller.session.open", endpoint),
            ("controller.session.send", message),
            ("controller.session.receive", ""),
            ("controller.session.close", ""),
        ]
    finally:
        await sdk.close()


async def test_next_typed_write_rechecks_grant_after_original_append_reply_wait(tmp_path):
    is_granted = True

    async def current(*_args):
        if not is_granted:
            raise PermissionError("revoked")

    sdk = await ControllerSdkConnection.connect(
        command(), config(tmp_path, "delay"), before_connect=allowed, before_dispatch=current
    )
    first = asyncio.create_task(sdk.session_operation("send", "first-original"))
    try:
        await wait_until(lambda: bool(calls(tmp_path)))
        second = asyncio.create_task(sdk.session_operation("send", "second-original"))
        await asyncio.sleep(0)
        assert not second.done()
        is_granted = False
        assert await first == "first-original"
        with pytest.raises(PermissionError):
            await second
        assert len(calls(tmp_path)) == 1
    finally:
        await sdk.close()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("cancel", [True, False])
async def test_uncertain_original_send_latches_withdrawal_even_after_late_reply(tmp_path, cancel):
    sdk = await connect(tmp_path, "delay", rpc_timeout_seconds=5 if cancel else 0.02)
    task = asyncio.create_task(sdk.session_operation("send", "original-input"))
    try:
        await wait_until(lambda: bool(calls(tmp_path)))
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
        else:
            with pytest.raises(ControllerSdkError) as failed:
                await task
            assert failed.value.is_outcome_uncertain
        await wait_until(lambda: not sdk._pending)
        with pytest.raises(ControllerSdkError, match="OUTCOME_UNKNOWN"):
            await sdk.session_operation("send", "replacement-or-replay")
        assert len(calls(tmp_path)) == 1
        # Non-effect native observation remains available on the same carrier.
        if not cancel:
            sdk._rpc_timeout_seconds = 5
        assert await sdk.call("managed_agent.get_session_v1", "{}", "") == "{}"
    finally:
        await sdk.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("operation", ["receive", "close"])
async def test_poll_and_close_never_accept_rebinding_payload(tmp_path, operation):
    sdk = await connect(tmp_path)
    try:
        with pytest.raises(ValueError):
            await sdk.session_operation(operation, '{"controller":"other"}')
        assert calls(tmp_path) == []
    finally:
        await sdk.close()


@pytest.mark.parametrize("verdict", [False, True, {}, "allow"])
async def test_typed_operation_requires_actual_assertion_before_private_dispatch(tmp_path, verdict):
    async def bad_assertion(*_args):
        return verdict

    sdk = await ControllerSdkConnection.connect(
        command(), config(tmp_path), before_connect=allowed, before_dispatch=bad_assertion
    )
    try:
        with pytest.raises(ControllerSdkError, match="DISPATCH_UNAVAILABLE") as failed:
            await sdk.session_operation("send", "original")
        assert failed.value.is_outcome_uncertain is False
        assert calls(tmp_path) == []
    finally:
        await sdk.close()
