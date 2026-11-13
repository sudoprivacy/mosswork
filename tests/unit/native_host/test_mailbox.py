"""Public ACP parser over actual private socketpair, fixture SDK session port."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from acp.connection import Connection
from acp.exceptions import RequestError

from octop.infra.native_host.cohost import CohostSessionEndpoint
from octop.infra.native_host.mailbox import ControllerMailboxStreams, MailboxOutcomeUnknown
from octop.infra.native_host.sdk import ControllerSdkConnection, ControllerSdkError

ENDPOINT = CohostSessionEndpoint(
    "channel", "agent", "controller", "/conversations/" + "a" * 32 + "/transcript"
)


class Sdk:
    def __init__(self) -> None:
        self.operations: list[tuple[str, str]] = []
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.error: Exception | None = None

    async def session_operation(self, operation: str, payload: str = "") -> str:
        self.operations.append((operation, payload))
        if operation == "send":
            await self.sent.put(json.loads(payload))
            if self.error is not None:
                raise self.error
        if operation == "receive":
            return await self.incoming.get()
        return ""

    @property
    def connection(self) -> ControllerSdkConnection:
        return cast(ControllerSdkConnection, self)


async def allowed() -> None:
    return None


async def test_actual_official_acp_parser_retains_original_error_data_and_correlation() -> None:
    sdk = Sdk()
    streams = await ControllerMailboxStreams.attach(
        sdk.connection, ENDPOINT, check_attachment=allowed
    )

    async def handler(method, params, notification):
        raise AssertionError("No reverse request in this test")

    acp = Connection(handler, streams.writer, streams.reader)
    try:
        task = asyncio.create_task(acp.send_request("session/load", {"sessionId": "original"}))
        sent = await sdk.sent.get()
        await sdk.incoming.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": sent["id"],
                    "error": {
                        "code": -32002,
                        "message": "Original unavailable",
                        "data": {"sessionId": "original", "text": "原始 🧩"},
                    },
                }
            )
        )
        with pytest.raises(RequestError) as error:
            await task
        assert error.value.code == -32002
        assert error.value.data == {"sessionId": "original", "text": "原始 🧩"}
        assert len([op for op in sdk.operations if op[0] == "send"]) == 1
        assert json.loads(sdk.operations[0][1])["controller"] == "controller"
    finally:
        await acp.close()
        await streams.close()


async def test_bidirectional_host_context_and_input_notifications_use_official_parser() -> None:
    sdk = Sdk()
    streams = await ControllerMailboxStreams.attach(
        sdk.connection, ENDPOINT, check_attachment=allowed
    )
    notifications = asyncio.Queue()

    async def handler(method, params, notification):
        if notification:
            await notifications.put((method, params))
            return None
        assert method == "_sudocode/session/host_context"
        assert params == {"sessionId": "original"}
        return {"appendSystemPrompt": "当前原始上下文 🧩"}

    acp = Connection(handler, streams.writer, streams.reader)
    try:
        await sdk.incoming.put("")  # Empty private poll must not close the ACP stream.
        await sdk.incoming.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "native-context",
                    "method": "_sudocode/session/host_context",
                    "params": {"sessionId": "original"},
                }
            )
        )
        answer = await sdk.sent.get()
        assert answer["id"] == "native-context"
        assert answer["result"] == {"appendSystemPrompt": "当前原始上下文 🧩"}
        await sdk.incoming.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "_sudocode/session/input_update",
                    "params": {"sessionId": "original", "update": {"clientId": "original-input"}},
                }
            )
        )
        assert await notifications.get() == (
            "_sudocode/session/input_update",
            {"sessionId": "original", "update": {"clientId": "original-input"}},
        )
    finally:
        await acp.close()
        await streams.close()


@pytest.mark.parametrize("verdict", [False, True, {}, "allow"])
async def test_missing_real_attachment_assertion_never_opens_mailbox(verdict) -> None:
    sdk = Sdk()

    async def invalid():
        return verdict

    with pytest.raises(RuntimeError):
        await ControllerMailboxStreams.attach(sdk.connection, ENDPOINT, check_attachment=invalid)
    assert sdk.operations == []


async def test_revoked_after_open_closes_only_original_transport_and_refuses_streams() -> None:
    sdk = Sdk()
    checked = 0

    async def current():
        nonlocal checked
        checked += 1
        if checked == 2:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        await ControllerMailboxStreams.attach(sdk.connection, ENDPOINT, check_attachment=current)
    assert [op[0] for op in sdk.operations] == ["open", "close"]


async def test_lost_append_reply_withdraws_original_stream_without_retry_or_new() -> None:
    sdk = Sdk()
    streams = await ControllerMailboxStreams.attach(
        sdk.connection, ENDPOINT, check_attachment=allowed
    )
    sdk.error = ControllerSdkError("OUTCOME_UNKNOWN", is_outcome_uncertain=True)
    try:
        streams.writer.write(b'{"jsonrpc":"2.0","id":1,"method":"session/prompt"}\n')
        await streams.writer.drain()
        await sdk.sent.get()
        with pytest.raises(MailboxOutcomeUnknown):
            await streams.reader.readline()
        assert streams.failure is not None
        assert len([op for op in sdk.operations if op[0] == "send"]) == 1
        assert len([op for op in sdk.operations if op[0] == "open"]) == 1
    finally:
        await streams.close()


@pytest.mark.parametrize("payload", ['{"jsonrpc":"1.0"}', '"not an object"', "malformed"])
async def test_malformed_received_message_refuses_public_stream_without_diagnostics(
    payload,
) -> None:
    sdk = Sdk()
    streams = await ControllerMailboxStreams.attach(
        sdk.connection, ENDPOINT, check_attachment=allowed
    )
    try:
        await sdk.incoming.put(payload)
        with pytest.raises(MailboxOutcomeUnknown) as error:
            await streams.reader.readline()
        assert payload not in repr(error.value)
        assert not [op for op in sdk.operations if op[0] == "send"]
    finally:
        await streams.close()


async def test_close_has_no_agent_cancel_or_join_receipt_and_preserves_first_error() -> None:
    sdk = Sdk()
    streams = await ControllerMailboxStreams.attach(
        sdk.connection, ENDPOINT, check_attachment=allowed
    )
    await sdk.incoming.put("malformed")
    with pytest.raises(MailboxOutcomeUnknown):
        await streams.reader.readline()
    original = streams.failure
    await streams.close()
    assert streams.failure is original
    assert sdk.operations[-1] == ("close", "")
    assert not any("cancel" in op[0] for op in sdk.operations)


@pytest.mark.parametrize("close_failure", [None, "error", "cancelled"])
async def test_socket_allocation_failure_closes_open_mailbox_and_preserves_primary(
    monkeypatch, close_failure
) -> None:
    import octop.infra.native_host.mailbox as mailbox

    sdk = Sdk()
    operation = sdk.session_operation
    original = OSError("fixture socket allocation failed")

    async def dispatch(name, payload=""):
        value = await operation(name, payload)
        if name == "close":
            if close_failure == "error":
                raise ControllerSdkError("OUTCOME_UNKNOWN", is_outcome_uncertain=True)
            if close_failure == "cancelled":
                raise asyncio.CancelledError("secondary cleanup cancellation")
        return value

    def unavailable(*args, **kwargs):
        raise original

    monkeypatch.setattr(sdk, "session_operation", dispatch)
    monkeypatch.setattr(mailbox.socket, "socketpair", unavailable)
    with pytest.raises(OSError) as failure:
        await ControllerMailboxStreams.attach(sdk.connection, ENDPOINT, check_attachment=allowed)
    assert failure.value is original
    assert [op[0] for op in sdk.operations] == ["open", "close"]


@pytest.mark.parametrize("cancel", [False, True])
async def test_partial_socket_setup_releases_acquired_resources_and_original_failure(
    monkeypatch, cancel
) -> None:
    import octop.infra.native_host.mailbox as mailbox

    sdk = Sdk()
    original_pair, original_open = mailbox.socket.socketpair, asyncio.open_connection
    operation = sdk.session_operation
    pairs, writers = [], []
    entered = asyncio.Event()
    original = OSError("fixture second stream failed")

    def pair(*args, **kwargs):
        acquired = original_pair(*args, **kwargs)
        pairs.extend(acquired)
        return acquired

    async def open_stream(*args, **kwargs):
        if not writers:
            result = await original_open(*args, **kwargs)
            writers.append(result[1])
            return result
        entered.set()
        if cancel:
            await asyncio.Future()
        raise original

    async def dispatch(name, payload=""):
        result = await operation(name, payload)
        if cancel and name == "close":
            raise asyncio.CancelledError("secondary cleanup cancellation")
        return result

    monkeypatch.setattr(mailbox.socket, "socketpair", pair)
    monkeypatch.setattr(asyncio, "open_connection", open_stream)
    monkeypatch.setattr(sdk, "session_operation", dispatch)
    task = asyncio.create_task(
        ControllerMailboxStreams.attach(sdk.connection, ENDPOINT, check_attachment=allowed)
    )
    await asyncio.wait_for(entered.wait(), 5)
    if cancel:
        task.cancel("original attachment cancellation")
        with pytest.raises(asyncio.CancelledError) as failure:
            await task
        assert task.cancelled()
        assert failure.value.args == ("original attachment cancellation",)
    else:
        with pytest.raises(OSError) as failure:
            await task
        assert failure.value is original
    assert [op[0] for op in sdk.operations] == ["open", "close"]
    assert len(writers) == 1 and writers[0].is_closing()
    assert len(pairs) == 2 and all(local.fileno() == -1 for local in pairs)
